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

token = os.environ.get('GITHUB_TOKEN')
client = httpx.AsyncClient()
semaphore = asyncio.Semaphore(50)
console = Console()


# 获取关注的仓库
async def get_followed_repos():
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    all_repo = []
    page = 0
    while True:
        page = page + 1
        url = "https://api.github.com/user/starred?page=" + str(page)
        response = await client.get(url=url, headers=headers)
        if response.status_code == 200:
            response = response.json()
            if len(response) == 0:
                break
            else:
                for repo in response:
                    all_repo.append(repo["full_name"])
    logger.info("获取到的 repo 为" + str(len(all_repo)))
    return all_repo


async def get_data(repo):
    async with semaphore:
        url = f"https://github.com/{repo}/releases.atom"
        response = await client.get(url=url)
        if response.status_code != 200:
            return {"repo": repo, "status": "无法获取数据"}

        rss = feedparser.parse(response.text).entries
        if not rss:
            return {"repo": repo, "status": "没有 release"}

        latest_release = rss[0]
        updated = latest_release['updated']
        updated_datetime = datetime.datetime.strptime(updated, '%Y-%m-%dT%H:%M:%SZ')
        updated_datetime = updated_datetime.replace(tzinfo=datetime.timezone.utc)
        
        now = datetime.datetime.now(datetime.timezone.utc)
        
        if now - updated_datetime > datetime.timedelta(days=365):
            return {
                "repo": repo,
                "status": "超过一年未更新",
                "latest_release": latest_release['title'],
                "last_updated": updated,
                "url": latest_release['link']
            }

        return None

async def main():
    all_repo = await get_followed_repos()
    tasks = [get_data(repo) for repo in all_repo]
    results = await asyncio.gather(*tasks)
    
    # 过滤掉 None 值并创建最终的 JSON 对象
    filtered_results = [result for result in results if result]
    json_output = json.dumps(filtered_results, ensure_ascii=False, indent=2)

    # 使用 rich 来美化输出
    syntax = Syntax(json_output, "json", theme="monokai", line_numbers=True)
    console.print(syntax)


if __name__ == "__main__":
    asyncio.run(main())
