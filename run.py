#!/usr/bin/env python3
"""百度贴吧自动签到 - 多账号 + WxPusher 推送

环境变量:
    BDUSS           多账号用 # 分隔
    STOKEN          多账号用 # 分隔（与 BDUSS 顺序一一对应，可留空）
    WXPUSHER_TOKEN  WxPusher 的 APP_TOKEN（ATP 开头）
    WXPUSHER_UID    WxPusher 的 UID（UID_ 开头）
"""

import logging
import os
import random
import time

import requests
from tieba_client import TiebaClient, best_result

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(asctime)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

WXPUSHER_TOKEN = os.environ.get("WXPUSHER_TOKEN", "").strip()
WXPUSHER_UID = os.environ.get("WXPUSHER_UID", "").strip()

MAX_ROUNDS = 5


def slow_sleeper(event: str, idx: int, total: int) -> None:
    """放慢版节流策略，防风控。

    单吧间隔 3-5 秒，每 10 个吧歇 15-25 秒。
    500 个吧约 40-50 分钟跑完，凌晨跑完全来得及。
    """
    if event == "step":
        if (idx + 1) % 10 == 0:
            extra = random.uniform(15, 25)
            logger.info(f"已签到 {idx + 1}/{total} 个，休息 {extra:.1f}s ...")
            time.sleep(extra)
        else:
            time.sleep(random.uniform(3.0, 5.0))
    elif event == "round":
        wait = random.uniform(60, 90)
        logger.info(f"第 {idx + 1} 轮重试结束，等待 {wait:.1f}s 后继续...")
        time.sleep(wait)


def run_signin(client: TiebaClient, tbs: str, forums: list, max_rounds: int = MAX_ROUNDS,
               sleeper=None) -> dict:
    """多轮签到主循环。"""
    if sleeper is None:
        sleeper = slow_sleeper

    queue = list(forums)
    stats = {"success": [], "already": [], "failed": []}

    for round_idx in range(max_rounds):
        if round_idx > 0:
            # 每轮重试前刷新 tbs，避免旧 tbs 导致验证失败
            new_tbs = client.get_tbs()
            if new_tbs is None:
                logger.error("轮间刷新 tbs 失败，剩余贴吧全部标记失败")
                stats["failed"].extend(f.get("name", "") for f in queue)
                break
            tbs = new_tbs
            sleeper("round", round_idx, len(queue))

        next_queue = []
        for idx, forum in enumerate(queue):
            if round_idx > 0 or idx > 0:
                sleeper("step", idx, len(queue))

            name = forum.get("name", "")
            fid = forum.get("id", "")
            result = client.sign_forum(fid, name, tbs)

            if result["status"] in ("retryable", "fatal") and client.stoken:
                web_result = client.sign_forum_web(name, tbs)
                result = best_result(result, web_result)

            if result["status"] == "retryable":
                next_queue.append(forum)
            elif result["status"] == "success":
                rank_str = f"，第 {result['rank']} 个签到" if result.get("rank") else ""
                logger.info(f"【{name}】签到成功{rank_str}")
                stats["success"].append(name)
            elif result["status"] == "already":
                logger.info(f"【{name}】{result['message']}")
                stats["already"].append(name)
            else:
                logger.error(f"【{name}】签到失败: {result['message']}")
                stats["failed"].append(name)

        queue = next_queue
        if not queue:
            break

    stats["failed"].extend(f.get("name", "") for f in queue)
    return stats


def build_summary(stats: dict, total: int) -> str:
    """生成单个账号的签到汇总。"""
    lines = [
        f"贴吧总数: {total}",
        f"✅ 签到成功: {len(stats['success'])}",
        f"⏭️ 已经签到: {len(stats['already'])}",
        f"❌ 签到失败: {len(stats['failed'])}",
    ]
    if stats["failed"]:
        lines.append("失败贴吧（多为被封的吧，属正常）:")
        lines.extend(f"  - {n}" for n in stats["failed"][:30])
        if len(stats["failed"]) > 30:
            lines.append(f"  ... 还有 {len(stats['failed']) - 30} 个")
    return "\n".join(lines)


def run_account(idx: int, bduss: str, stoken: str) -> str:
    """处理单个账号，返回报告文本。"""
    header = f"【账号 {idx + 1}】"
    logger.info(f"========== {header} 开始 ==========")
    try:
        client = TiebaClient(bduss, stoken=stoken)

        tbs = client.get_tbs()
        if not tbs:
            logger.error(f"{header}获取 tbs 失败，BDUSS 可能失效")
            return f"{header}\n⚠️ 获取 tbs 失败，BDUSS 可能失效，本次未签到。"

        forums = client.get_favorites()
        if not forums:
            return f"{header}\n⚠️ 未获取到关注的贴吧。"

        total = len(forums)
        logger.info(f"{header}共 {total} 个贴吧，开始签到")
        stats = run_signin(client, tbs, forums)
        return f"{header}\n" + build_summary(stats, total)
    except Exception as e:
        logger.exception(f"{header}执行异常")
        return f"{header}\n💥 执行异常: {e}"


def send_wxpusher(content: str) -> bool:
    """通过 WxPusher 推送签到报告。"""
    if not WXPUSHER_TOKEN or not WXPUSHER_UID:
        logger.warning("未配置 WXPUSHER_TOKEN / WXPUSHER_UID，跳过推送")
        return False
    try:
        r = requests.post(
            "https://wxpusher.zjiecode.com/api/send/message",
            json={
                "appToken": WXPUSHER_TOKEN,
                "content": content,
                "summary": "贴吧签到结果",
                "contentType": 1,
                "uids": [WXPUSHER_UID],
            },
            timeout=15,
        )
        logger.info(f"WxPusher 响应: {r.text[:200]}")
        return r.ok
    except Exception as e:
        logger.error(f"WxPusher 推送失败: {e}")
        return False


def main() -> None:
    bdusses = [b.strip() for b in os.environ.get("BDUSS", "").split("#") if b.strip()]
    stokens = [s.strip() for s in os.environ.get("STOKEN", "").split("#") if s.strip()]
    if not bdusses:
        raise SystemExit("未配置 BDUSS 环境变量")

    reports = []
    for i, bduss in enumerate(bdusses):
        stoken = stokens[i] if i < len(stokens) else ""
        reports.append(run_account(i, bduss, stoken))
        # 多账号之间多歇一会，避免两个账号同 IP 连续高频
        if i < len(bdusses) - 1:
            pause = random.uniform(30, 60)
            logger.info(f"账号 {i + 1} 完成，休息 {pause:.0f}s 后处理下一个账号...")
            time.sleep(pause)

    content = "📋 贴吧签到报告\n\n" + "\n\n".join(reports)
    logger.info("\n" + content)
    send_wxpusher(content)


if __name__ == "__main__":
    main()
