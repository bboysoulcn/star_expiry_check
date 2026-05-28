### 获取github中已经star项目
import httpx
import time
from loguru import logger
import asyncio
import feedparser
import html2text
import datetime
from rich.console import Console
from rich.syntax import Syntax
import json
import os
from pathlib import Path

token = os.environ.get('TOKEN')
# 优化连接池和超时配置
client = httpx.AsyncClient(
    timeout=30.0,
    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20)
)
# 降低并发数避免 rate limit（从 100 改为 20）
semaphore = asyncio.Semaphore(20)
console = Console()

# 缓存配置
CACHE_FILE = Path("cache_starred_repos.json")
CACHE_EXPIRY_HOURS = 24


def load_cache():
    """从缓存文件加载数据"""
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            cache_data = json.load(f)
            cache_time = datetime.datetime.fromisoformat(cache_data.get('timestamp', ''))
            if datetime.datetime.now() - cache_time < datetime.timedelta(hours=CACHE_EXPIRY_HOURS):
                logger.info(f"使用缓存数据，缓存时间: {cache_time}")
                repos = cache_data.get('repos', [])
                # 兼容旧缓存格式（纯字符串列表 → dict 列表）
                if repos and isinstance(repos[0], str):
                    repos = [{"full_name": r, "stars": 0} for r in repos]
                return repos
    except Exception as e:
        logger.warning(f"读取缓存失败: {e}")
    return None


def save_cache(repos):
    """保存数据到缓存文件"""
    try:
        cache_data = {
            'timestamp': datetime.datetime.now().isoformat(),
            'repos': repos
        }
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        logger.info("已保存缓存数据")
    except Exception as e:
        logger.warning(f"保存缓存失败: {e}")


# 获取关注的仓库（优化版：使用缓存 + 并行获取多页）
async def get_followed_repos():
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    # 尝试从缓存加载
    cached_repos = load_cache()
    if cached_repos:
        logger.info(f"从缓存获取到 {len(cached_repos)} 个仓库")
        return cached_repos
    
    # 首先获取第一页，每页100个（最大值）
    url = "https://api.github.com/user/starred?per_page=100&page=1"
    response = await client.get(url=url, headers=headers)
    
    if response.status_code != 200:
        logger.error(f"API 请求失败: {response.status_code}")
        return []
    
    all_repo = []
    first_page = response.json()
    all_repo.extend([{"full_name": repo["full_name"], "stars": repo.get("stargazers_count", 0)} for repo in first_page])
    
    # 检查是否有更多页
    link_header = response.headers.get('Link', '')
    if 'rel="last"' in link_header:
        # 解析最后一页的页码
        import re
        last_page_match = re.search(r'page=(\d+)>; rel="last"', link_header)
        if last_page_match:
            last_page = int(last_page_match.group(1))
            logger.info(f"检测到 {last_page} 页数据，开始并行获取...")
            
            # 并行获取剩余页面
            async def fetch_page(page_num):
                url = f"https://api.github.com/user/starred?per_page=100&page={page_num}"
                try:
                    resp = await client.get(url=url, headers=headers)
                    if resp.status_code == 200:
                        return [{"full_name": repo["full_name"], "stars": repo.get("stargazers_count", 0)} for repo in resp.json()]
                except Exception as e:
                    logger.error(f"获取第 {page_num} 页失败: {e}")
                return []
            
            # 分批并行获取（每批10页）
            batch_size = 10
            for i in range(2, last_page + 1, batch_size):
                batch_pages = range(i, min(i + batch_size, last_page + 1))
                tasks = [fetch_page(page) for page in batch_pages]
                results = await asyncio.gather(*tasks)
                for repos in results:
                    all_repo.extend(repos)
                logger.info(f"已获取 {len(all_repo)} 个仓库...")
    
    logger.info(f"总共获取到 {len(all_repo)} 个仓库")
    
    # 保存到缓存
    save_cache(all_repo)
    
    return all_repo


async def get_data(repo_info):
    """获取仓库发布信息和 star 数量，带重试机制"""
    async with semaphore:
        repo_name = repo_info["full_name"]
        stars = repo_info.get("stars", 0)
        url = f"https://github.com/{repo_name}/releases.atom"
        max_retries = 3
        
        # 预先判断 star 情况
        star_low = stars < 1000
        
        for attempt in range(max_retries):
            try:
                response = await client.get(url=url, follow_redirects=True)
                
                if response.status_code != 200:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1 * (attempt + 1))
                        continue
                    result = {"repo": repo_name, "repo_url": f"https://github.com/{repo_name}", "stars": stars, "status": "无法获取数据"}
                    if star_low:
                        result["status"] = "无法获取数据且star数目小于1k"
                    return result

                rss = feedparser.parse(response.text).entries
                if not rss:
                    result = {"repo": repo_name, "repo_url": f"https://github.com/{repo_name}", "stars": stars, "status": "没有 release"}
                    if star_low:
                        result["status"] = "没有 release且star数目小于1k"
                    return result

                latest_release = rss[0]
                updated = latest_release['updated']
                updated_datetime = datetime.datetime.strptime(updated, '%Y-%m-%dT%H:%M:%SZ')
                updated_datetime = updated_datetime.replace(tzinfo=datetime.timezone.utc)
                
                now = datetime.datetime.now(datetime.timezone.utc)
                expired = now - updated_datetime > datetime.timedelta(days=365)
                
                if expired or star_low:
                    result = {
                        "repo": repo_name,
                        "repo_url": f"https://github.com/{repo_name}",
                        "stars": stars,
                    }
                    issues = []
                    if expired:
                        issues.append("超过一年未更新")
                        result["latest_release"] = latest_release['title']
                        result["last_updated"] = updated
                        result["url"] = latest_release['link']
                    if star_low:
                        issues.append("star数目小于1k")
                    result["status"] = "且".join(issues)
                    return result

                return None
                
            except Exception as e:
                logger.warning(f"处理 {repo_name} 时出错 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))
                else:
                    result = {"repo": repo_name, "repo_url": f"https://github.com/{repo_name}", "stars": stars, "status": f"处理失败: {str(e)}"}
                    if star_low:
                        result["status"] = f"star数目小于1k且处理失败: {str(e)}"
                    return result

async def main():
    start_time = time.time()
    
    logger.info("开始获取 starred 仓库列表...")
    all_repo = await get_followed_repos()
    
    if not all_repo:
        logger.error("未能获取到任何仓库")
        return
    
    logger.info(f"开始检查 {len(all_repo)} 个仓库的更新状态...")
    
    # 分批处理以避免内存问题（每批500个）
    batch_size = 500
    all_filtered_results = []
    
    for i in range(0, len(all_repo), batch_size):
        batch = all_repo[i:i + batch_size]
        logger.info(f"处理批次 {i//batch_size + 1}/{(len(all_repo)-1)//batch_size + 1} ({len(batch)} 个仓库)")
        
        tasks = [get_data(repo) for repo in batch]
        results = await asyncio.gather(*tasks)
        
        # 过滤掉 None 值
        filtered_results = [result for result in results if result]
        all_filtered_results.extend(filtered_results)
        
        logger.info(f"本批次发现 {len(filtered_results)} 个需要关注的仓库")
    
    # 输出结果
    json_output = json.dumps(all_filtered_results, ensure_ascii=False, indent=2)
    
    # 使用 rich 来美化输出
    syntax = Syntax(json_output, "json", theme="monokai", line_numbers=True)
    console.print(syntax)
    
    elapsed_time = time.time() - start_time
    logger.info(f"总共发现 {len(all_filtered_results)} 个需要关注的仓库（超过一年未更新或 star 不足 1k）")
    logger.info(f"总耗时: {elapsed_time:.2f} 秒")
    
    # 清理连接
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
