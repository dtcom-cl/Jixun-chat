"""人类好友会话（用户 2026-09-27 第七轮要求）。

    「加好友页面分两块：上半添加 AI 好友，下半添加人类好友 —— 只填对方 4 位账号，
      点添加。不需要对方同意，输入账号即双向生效。」
    「人类会话里能正常收发文字/图片，不经 AI、不走延迟队列，直接投递；
      对方不在线就落库，他上线 resume 时补发。」
    「人类好友不能拉群。」

所以这个模块只做一件事：**把一条消息从 A 的手机送到 B 的手机**。

为什么不复用 dispatcher 的延迟队列：
延迟队列存在的意义是「让 AI 像个真人一样拖一会儿再回」。人对人发消息没有
任何理由拖 —— 用户按下发送，对方就该立刻收到（或离线时立刻入库等着）。
把它塞进延迟队列只会引入一堆无意义的 task，还会被「催」机制误伤。

投递失败（对方不在线）时消息保持 `sent` 状态且计入未读，等对方上线
发 resume 时由 ws_gateway 的补发逻辑带出去 —— 与 AI 消息同一条通道，
客户端不需要为人类消息写第二套逻辑。
"""
from __future__ import annotations

import logging

import database as db
import ws_gateway as gw

logger = logging.getLogger("human_chat")

# 人类会话在 conversations.type 里的取值。
HUMAN_CONVERSATION_TYPE = "human"


def ensure_conversation(user_id: int, peer_user_id: int) -> int:
    """确保两个人之间有一条人类会话，返回会话 id（幂等）。

    **两边各一条**：A 的列表里显示「B」，B 的列表里显示「A」。
    不做成共享一条会话，是因为 conversations 表有 user_id 归属列，
    而 /api/conversations 是按 user_id 过滤的 —— 共享一条的话，
    只有一方能在自己的列表里看到它。
    """
    mine = db.find_human_conversation(user_id, peer_user_id)
    if mine is None:
        peer_row = db.get_user(peer_user_id)
        title = (peer_row["display_name"] if peer_row else "") or f"用户{peer_user_id}"
        mine = db.create_conversation(
            user_id=user_id,
            conv_type=HUMAN_CONVERSATION_TYPE,
            title=title,
            participants=[],
            peer_user_id=peer_user_id,
        )
    theirs = db.find_human_conversation(peer_user_id, user_id)
    if theirs is None:
        my_row = db.get_user(user_id)
        title = (my_row["display_name"] if my_row else "") or f"用户{user_id}"
        db.create_conversation(
            user_id=peer_user_id,
            conv_type=HUMAN_CONVERSATION_TYPE,
            title=title,
            participants=[],
            peer_user_id=user_id,
        )
    return int(mine)


def peer_conversation_id(user_id: int, peer_user_id: int) -> int | None:
    """从「对方的视角」看这条会话的 id（往那边推消息时要用对方的会话 id）。"""
    return db.find_human_conversation(peer_user_id, user_id)


async def deliver_human_message(
    *,
    conversation_id: int,
    sender_user_id: int,
    content: str,
    content_type: str = "text",
    timestamp: str = "",
    msg_id: str = "",
) -> None:
    """把用户发的一条消息投递给人类好友。

    这个函数在 dispatcher.handle_user_send 里被调用 —— 那时消息**已经**写进
    messages 表了（用的是发送方视角的 conversation_id），所以这里只做两件事：

    1. 在对方那条会话里再写一份，这样对方拉历史消息时能看到；
    2. 对方在线就立刻推过去（WS message），离线就留着等他 resume。

    为什么要在对方会话里重写一份而不是共享一份：
    见 ensure_conversation 的说明 —— 会话按 user_id 归属，共享会让一方看不见。
    两份内容完全相同（含 msg_id），客户端按 msg_id 去重，不会出现双份气泡。
    """
    conversation = db.get_conversation(conversation_id)
    if conversation is None:
        return
    peer_user_id = conversation["peer_user_id"]
    if peer_user_id is None:
        logger.warning("人类会话 %s 没有 peer_user_id，无法投递", conversation_id)
        return

    ts = timestamp or db.now_iso()
    peer_conv_id = peer_conversation_id(sender_user_id, int(peer_user_id))
    if peer_conv_id is None:
        # 对方那条会话没建起来（比如他删了好友又加回来时中断过）：补上。
        peer_conv_id = ensure_conversation(int(peer_user_id), sender_user_id)
        peer_conv_id = peer_conversation_id(sender_user_id, int(peer_user_id)) or peer_conv_id

    sender_row = db.get_user(sender_user_id)
    sender_name = (sender_row["display_name"] if sender_row else "") or "好友"

    # 1. 在对方会话里落一份（对方不在线时靠它 resume 补发）。
    #
    # **必须用新的 msg_id**：`messages.msg_id` 上有全局唯一索引
    # （`idx_messages_msg_id`），发送方那条已经占用了原 id，这里再插一次
    # 会被 `INSERT OR IGNORE` 静默丢掉 —— 现象就是「消息发出去了，对方收不到」，
    # 而且完全不报错。踩过一次，冒烟测试抓出来的。
    #
    # 两边本来也是两条独立记录（各自归属不同的 conversation，落在不同设备上），
    # 用各自的 id 才符合 ack / mark_delivered 的语义。原 id 记进 client_msg_id，
    # 排障时还能顺着它把两边对上。
    row = db.insert_message(
        conversation_id=int(peer_conv_id),
        sender_type="user",
        sender_id=sender_user_id,
        content=content,
        timestamp=ts,
        content_type=content_type,
        state="sent",
        client_msg_id=msg_id or None,
    )
    # 对方未读 +1（PRD 3.1.3 小红点）。注意这里加的是**对方**那条会话的未读数。
    db.touch_conversation(int(peer_conv_id), ts, unread_delta=1)
    if row is None:
        logger.warning("人类消息写入对方会话失败（msg_id 冲突）")
        return

    # 2. 在线就立刻推（离线时消息已在库里，等他 resume 补发）。
    payload = db.message_row_to_ws(row)
    payload["sender_name"] = sender_name
    payload["is_human"] = True
    ok = await gw.push_message_to_user(int(peer_user_id), payload)
    logger.info(
        "人类消息投递：from=%s to=%s conversation=%s online=%s content=%s",
        sender_user_id,
        peer_user_id,
        peer_conv_id,
        ok,
        content[:40],
    )