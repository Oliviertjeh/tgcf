"""The module responsible for operating tgcf in live mode."""

import logging
import os
import sys
from typing import Union, Optional, Dict, List

from telethon import TelegramClient, events, functions, types
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message

from tgcf import config, const
from tgcf import storage as st
from tgcf.bot import get_events
from tgcf.config import CONFIG, get_SESSION
from tgcf.plugins import apply_plugins, load_async_plugins
from tgcf.utils import clean_session_files

async def send_message(client: TelegramClient, chat_id: Union[int, str], message: types.Message, topic_id: Optional[int] = None) -> types.Message:
    """Send a message to the specified chat, optionally to a topic."""
    try:
        if topic_id:
            return await client.send_message(chat_id, message, message_thread_id=topic_id)
        else:
            return await client.send_message(chat_id, message)
    except Exception as e:
        logging.error(f"Error sending message to {chat_id} (topic_id: {topic_id}): {e}")
        return None

async def new_message_handler(event: Union[Message, events.NewMessage]) -> None:
    """Process new incoming messages."""
    chat_id = event.chat_id

    if chat_id not in config.from_to:
        return
    logging.info(f"New message received in {chat_id}")
    message = event.message

    event_uid = st.EventUid(event)

    length = len(st.stored)
    exceeding = length - const.KEEP_LAST_MANY

    if exceeding > 0:
        for key in st.stored:
            del st.stored[key]
            break

    destinations = config.from_to.get(chat_id)

    tm = await apply_plugins(message)
    if not tm:
        return

    if event.is_reply:
        r_event = st.DummyEvent(chat_id, event.reply_to_msg_id)
        r_event_uid = st.EventUid(r_event)

    st.stored[event_uid] = {}
    for dest_info in destinations:
        topic_id = None
        destination_chat_id = None

        if isinstance(dest_info, dict):
            destination_chat_id = dest_info.get("chat_id")
            topic_id = dest_info.get("topic_id")
        elif isinstance(dest_info, int):
            destination_chat_id = dest_info

        if destination_chat_id is not None:
            if event.is_reply and r_event_uid in st.stored:
                tm.reply_to = st.stored.get(r_event_uid).get(destination_chat_id)

            fwded_msg = await send_message(event.client, destination_chat_id, tm, topic_id=topic_id)
            if fwded_msg:
                st.stored[event_uid].update({destination_chat_id: fwded_msg})
    tm.clear()


async def edited_message_handler(event) -> None:
    """Handle message edits."""
    message = event.message

    chat_id = event.chat_id

    if chat_id not in config.from_to:
        return

    logging.info(f"Message edited in {chat_id}")

    event_uid = st.EventUid(event)

    tm = await apply_plugins(message)

    if not tm:
        return

    fwded_msgs = st.stored.get(event_uid)

    if fwded_msgs:
        for _, msg in fwded_msgs.items():
            if config.CONFIG.live.delete_on_edit == message.text:
                await msg.delete()
                await message.delete()
            else:
                await msg.edit(tm.text)
        return

    destinations = config.from_to.get(chat_id)

    for dest_info in destinations:
        topic_id = None
        destination_chat_id = None

        if isinstance(dest_info, dict):
            destination_chat_id = dest_info.get("chat_id")
            topic_id = dest_info.get("topic_id")
        elif isinstance(dest_info, int):
            destination_chat_id = dest_info

        if destination_chat_id is not None:
            await send_message(event.client, destination_chat_id, tm, topic_id=topic_id)
    tm.clear()


async def deleted_message_handler(event):
    """Handle message deletes."""
    chat_id = event.chat_id
    if chat_id not in config.from_to:
        return

    logging.info(f"Message deleted in {chat_id}")

    event_uid = st.EventUid(event)
    fwded_msgs = st.stored.get(event_uid)
    if fwded_msgs:
        for _, msg in fwded_msgs.items():
            await msg.delete()
        return


ALL_EVENTS = {
    "new": (new_message_handler, events.NewMessage()),
    "edited": (edited_message_handler, events.MessageEdited()),
    "deleted": (deleted_message_handler, events.MessageDeleted()),
}


async def start_sync() -> None:
    """Start tgcf live sync."""
    # clear past session files
    clean_session_files()

    # load async plugins defined in plugin_models
    await load_async_plugins()

    SESSION = get_SESSION()
    client = TelegramClient(
        SESSION,
        CONFIG.login.API_ID,
        CONFIG.login.API_HASH,
        sequential_updates=CONFIG.live.sequential_updates,
    )
    if CONFIG.login.user_type == 0:
        if CONFIG.login.BOT_TOKEN == "":
            logging.warning("Bot token not found, but login type is set to bot.")
            sys.exit()
        await client.start(bot_token=CONFIG.login.BOT_TOKEN)
    else:
        await client.start()
    config.is_bot = await client.is_bot()
    logging.info(f"config.is_bot={config.is_bot}")
    command_events = get_events()

    await config.load_admins(client)

    ALL_EVENTS.update(command_events)

    for key, val in ALL_EVENTS.items():
        if config.CONFIG.live.delete_sync is False and key == "deleted":
            continue
        client.add_event_handler(*val)
        logging.info(f"Added event handler for {key}")

    if config.is_bot and const.REGISTER_COMMANDS:
        await client(
            functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(),
                lang_code="en",
                commands=[
                    types.BotCommand(command=key, description=value)
                    for key, value in const.COMMANDS.items()
                ],
            )
        )
    config.from_to = await config.load_from_to(client, config.CONFIG.forwards)
    await client.run_until_disconnected()
