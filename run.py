#!/usr/bin/env python3
"""百度贴吧自动签到 - 多账号 + WxPusher 推送

环境变量:
    BDUSS           多账号用 # 分隔
    STOKEN          多账号用 # 分隔（与 BDUSS 顺序一一对应，可留空）
    WXPUSHER_TOKEN  WxPusher 的 APP_TOKEN（ATP 开头）
    WXPUSHER_UID    WxPusher 的 UID（UID_ 开头）
    TEST_LIMIT      可选，调试用：每个账号只签到前 N 个贴吧，快速验证流程

注意: WxPusher 已下线微信公众号通道，消息会推送到 WxPusher APP，需安装其 APP 接收。
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
try:
    TEST_LIMIT = int(os.environ.get("TEST_LIMIT", "0") or 0)
except ValueError:
    TEST_LIMIT = 0

MAX_ROUNDS = 5

# ---------- 节流参数 ----------
SIGN_DELAY = (3.0, 5.0)      # 真实签到动作后的间隔（防风控核心，不要调小）
ALREADY_DELAY = (0.3, 1.0)   # 仅查询（已签到）后的间隔，可以快
BATCH_REST = (5.0, 10.0)     # 每处理 10 个的额外休息
ROUND_WAIT = (60.0, 90.0)    # 重试轮次之间的间隔

WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"


def run_signin(client: TiebaClient, tbs: str, forums: list, max_rounds: int = MAX_ROUNDS) -> dict:
    """多轮签到主循环，自适应节流。"""
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
            wait = random.uniform(*ROUND_WAIT)
            logger.info(f"第 {round_idx} 轮结束，等待 {wait:.0f}s 后重试剩余 {len(queue)} 个...")
            time.sleep(wait)

        next_queue = []
        for idx, forum in enumerate(queue):
            name = forum.get("name", "")
            fid = forum.get("id", "")
            result = client.sign_forum(fid, name, tbs)

            if result["status"] in ("retryable", "fatal") and client.stoken:
                web_result = client.sign_forum_web(name, tbs)
                result = best_result(result, web_result)

            if result["status"] == "retryable":
                logger.warning(f"【{name}】暂时失败（第 {round_idx + 1} 轮）: {result['message']}，稍后自动重签")
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

            # ---------- 自适应节流 ----------
            if result["status"] == "already":
                # 纯查询，没做签到动作，快速通过
                time.sleep(random.uniform(*ALREADY_DELAY))
            else:
                # 真实请求（无论成功失败），保持防风控间隔
                time.sleep(random.uniform(*SIGN_DELAY))
            if (idx + 1) % 10 == 0 and idx + 1 < len(queue):
                rest = random.uniform(*BATCH_REST)
                logger.info(f"本轮已处理 {idx + 1}/{len(queue)} 个，休息 {rest:.1f}s ...")
                time.sleep(rest)

        queue = next_queue
        if not queue:
            break

    stats["failed"].extend(f.get("name", "") for f in queue)
    return stats


def build_summary(stats: dict, total: int) -> str:
    """生成单个账号的签到汇总（Markdown 格式）。"""
    lines = [
        f"**贴吧总数**: {total}",
        f"**✅ 签到成功**: {len(stats['success'])}",
        f"**⏭️ 已经签到**: {len(stats['already'])}",
        f"**❌ 签到失败**: {len(stats['failed'])}",
    ]
    if stats["failed"]:
        lines.append("")
        lines.append("**失败贴吧**（多为被封的吧，属正常）:")
        lines.extend(f"- {n}" for n in stats["failed"][:30])
        if len(stats["failed"]) > 30:
            lines.append(f"- ... 还有 {len(stats['failed']) - 30} 个")
    return "\n".join(lines)


def run_account(idx: int, bduss: str, stoken: str) -> str:
    """处理单个账号，返回报告文本。"""
    header = f"## 【账号 {idx + 1}】"
    logger.info(f"========== {header} 开始 ==========")
    try:
        client = TiebaClient(bduss, stoken=stoken)

        tbs = client.get_tbs()
        if not tbs:
            logger.error(f"{header}获取 tbs 失败，BDUSS 可能失效")
            return f"{header}\n\n⚠️ 获取 tbs 失败，BDUSS 可能失效，本次未签到。"

        forums = client.get_favorites()
        if not forums:
            return f"{header}\n\n⚠️ 未获取到关注的贴吧。"

        if TEST_LIMIT > 0:
            forums = forums[:TEST_LIMIT]
            logger.info(f"{header}TEST_LIMIT 生效，仅签到前 {TEST_LIMIT} 个贴吧")

        total = len(forums)
        logger.info(f"{header}共 {total} 个贴吧，开始签到")
        stats = run_signin(client, tbs, forums)
        return f"{header}\n\n" + build_summary(stats, total)
    except Exception as e:
        logger.exception(f"{header}执行异常")
        return f"{header}\n\n💥 执行异常: {e}"


def send_wxpusher(content: str) -> bool:
    """通过 WxPusher 推送签到报告（contentType 3 = Markdown，在 APP 内渲染）。

    成功判定: 响应 JSON 的 code == 1000。
    注意: WxPusher 微信公众号通道已下线，消息只会推到 WxPusher APP。
    """
    if not WXPUSHER_TOKEN or not WXPUSHER_UID:
        logger.warning("未配置 WXPUSHER_TOKEN / WXPUSHER_UID，跳过推送")
        return False
    try:
        r = requests.post(
            WXPUSHER_API,
            json={
                "appToken": WXPUSHER_TOKEN,
                "content": content,
                "summary": "贴吧签到结果",
                "contentType": 3,
                "uids": [WXPUSHER_UID],
            },
            timeout=15,
        )
        body = r.json()
        ok = body.get("code") == 1000
        logger.info(f"WxPusher 响应: {str(body)[:200]}")
        if not ok:
            logger.error(f"WxPusher 推送失败: {body.get('msg', '未知错误')}")
        return ok
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

    content = "# 📋 贴吧签到报告\n\n" + "\n\n".join(reports)
    logger.info("\n" + content.replace("#", "").replace("**", ""))
    send_wxpusher(content)


if __name__ == "__main__":
    main()
