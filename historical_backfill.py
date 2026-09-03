"""Historical backfill scheduler for Telegram automations.

Processes messages that already existed in a source chat when the feature is
activated, at a configurable interval, without interfering with live delivery.
State is persisted locally per Telegram session.
"""

import asyncio
import contextvars
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from types import SimpleNamespace


active_automation_id = contextvars.ContextVar(
    "historical_backfill_automation_id",
    default="",
)


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {
        "1", "true", "yes", "on", "sim", "enabled", "active"
    }


def enabled(automation):
    if not isinstance(automation, dict):
        return False
    return _truthy(
        automation.get("history_backfill_enabled")
        or automation.get("historical_backfill_enabled")
        or automation.get("backfill_enabled")
    )


def interval_seconds(automation):
    raw_seconds = automation.get("history_interval_seconds")
    if raw_seconds not in (None, ""):
        try:
            return max(60, min(int(float(raw_seconds)), 7 * 24 * 3600))
        except (TypeError, ValueError):
            pass

    raw_minutes = (
        automation.get("history_interval_minutes")
        or automation.get("backfill_interval_minutes")
        or 60
    )
    try:
        minutes = float(raw_minutes)
    except (TypeError, ValueError):
        minutes = 60.0
    return max(60, min(int(minutes * 60), 7 * 24 * 3600))


def start_mode(automation):
    raw = str(
        automation.get("history_start_mode")
        or automation.get("backfill_start_mode")
        or "first"
    ).strip().lower()
    if raw in {"date", "data"}:
        return "date"
    if raw in {"message_id", "message", "id"}:
        return "message_id"
    return "first"


def _parse_datetime(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _automation_id(automation):
    return str(automation.get("id") or "").strip()


def _settings_signature(automation):
    raw = "|".join([
        str(automation.get("source_chat_id") or ""),
        str(automation.get("destination_chat_id") or ""),
        start_mode(automation),
        str(automation.get("history_start_date") or ""),
        str(automation.get("history_start_message_id") or ""),
        str(automation.get("history_backfill_reset_key") or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _state_file(session_key):
    base = os.path.dirname(os.path.abspath(__file__))
    configured = os.getenv("TELEGRAM_HISTORY_BACKFILL_STATE_FILE", "").strip()
    if configured:
        return configured
    return os.path.join(base, f"history_backfill_state_{session_key}.json")


def _load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_state(path, state):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="history_backfill_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _source_value(value):
    text = str(value or "").strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return text


async def _initial_cursor(worker, source_entity, automation):
    mode = start_mode(automation)
    if mode == "message_id":
        try:
            requested = int(automation.get("history_start_message_id") or 1)
        except (TypeError, ValueError):
            requested = 1
        return max(0, requested - 1)

    if mode == "date":
        date_value = _parse_datetime(automation.get("history_start_date"))
        if date_value is not None:
            # Find the newest message strictly before the chosen date. The next
            # ascending message after this cursor is therefore the first eligible.
            previous = await worker.client.get_messages(
                source_entity,
                limit=1,
                offset_date=date_value,
            )
            if previous:
                return int(previous[0].id)
    return 0


async def _snapshot_end(worker, source_entity):
    latest = await worker.client.get_messages(source_entity, limit=1)
    return int(latest[0].id) if latest else 0


async def _ensure_automation_state(worker, state, automation, session_key):
    automation_id = _automation_id(automation)
    source = _source_value(automation.get("source_chat_id"))
    source_entity = await worker.resolve_destination_entity(source)
    signature = _settings_signature(automation)
    current = state.get(automation_id)

    if not isinstance(current, dict) or current.get("signature") != signature:
        cursor = await _initial_cursor(worker, source_entity, automation)
        end_message_id = await _snapshot_end(worker, source_entity)
        current = {
            "signature": signature,
            "cursor_message_id": int(cursor),
            "end_message_id": int(end_message_id),
            "next_run_at": 0,
            "completed": False,
            "initialized_at": int(time.time()),
        }
        state[automation_id] = current
        print(
            f"[History:{session_key}] inicializada automação={automation_id} "
            f"cursor={cursor} fim={end_message_id} intervalo={interval_seconds(automation)}s"
        )
    return current, source_entity


async def _next_batch(worker, source_entity, cursor, end_message_id):
    messages = []
    async for message in worker.client.iter_messages(
        source_entity,
        min_id=int(cursor),
        reverse=True,
        limit=20,
    ):
        if int(message.id) > int(end_message_id):
            break
        messages.append(message)

    if not messages:
        return []

    first = messages[0]
    grouped_id = getattr(first, "grouped_id", None)
    if not grouped_id:
        return [first]

    album = [
        item for item in messages
        if getattr(item, "grouped_id", None) == grouped_id
    ]
    album.sort(key=lambda item: int(item.id))
    return album or [first]


async def _already_published(worker, automation, source_id, messages):
    automation_id = _automation_id(automation)
    for message in messages:
        links = await worker.find_message_links(
            source_chat_id=source_id,
            source_message_id=message.id,
            automation_id=automation_id,
        )
        if links:
            return True
    return False


async def _publish_unit(worker, automation, source_id, messages):
    if len(messages) == 1 and not getattr(messages[0], "grouped_id", None):
        await worker.publish_single_message(messages[0], source_id, automation)
        return

    grouped_id = getattr(messages[0], "grouped_id", None)
    token = active_automation_id.set(_automation_id(automation))
    try:
        await worker.album_handler(
            SimpleNamespace(
                chat_id=source_id,
                grouped_id=grouped_id,
                messages=messages,
                out=any(bool(getattr(item, "out", False)) for item in messages),
            )
        )
    finally:
        active_automation_id.reset(token)


async def _process_automation(worker, state, automation, session_key, state_path):
    automation_id = _automation_id(automation)
    if not automation_id:
        return

    item, source_entity = await _ensure_automation_state(
        worker, state, automation, session_key
    )

    if item.get("completed"):
        return

    now = time.time()
    if float(item.get("next_run_at") or 0) > now:
        return

    source_id = _source_value(automation.get("source_chat_id"))
    cursor = int(item.get("cursor_message_id") or 0)
    end_message_id = int(item.get("end_message_id") or 0)

    # Skip already-published units immediately, so they do not consume the user
    # configured interval. Cap the scan to avoid hogging the event loop.
    for _ in range(25):
        messages = await _next_batch(worker, source_entity, cursor, end_message_id)
        if not messages:
            item["completed"] = True
            item["next_run_at"] = 0
            _save_state(state_path, state)
            print(f"[History:{session_key}] concluída automação={automation_id}")
            return

        unit_last_id = max(int(message.id) for message in messages)
        if await _already_published(worker, automation, source_id, messages):
            cursor = unit_last_id
            item["cursor_message_id"] = cursor
            _save_state(state_path, state)
            continue

        await _publish_unit(worker, automation, source_id, messages)

        item["cursor_message_id"] = unit_last_id
        item["last_published_at"] = int(time.time())
        item["next_run_at"] = time.time() + interval_seconds(automation)
        _save_state(state_path, state)

        print(
            f"[History:{session_key}] automação={automation_id} "
            f"origem_msg={messages[0].id}-{unit_last_id} "
            f"próximo_em={interval_seconds(automation)}s"
        )
        return


async def run(worker, load_automations, session_key):
    """Run forever inside the existing session_worker process."""
    state_path = _state_file(session_key)
    state = _load_state(state_path)

    while True:
        try:
            if not worker.client.is_connected():
                await asyncio.sleep(5)
                continue

            automations = await load_automations()
            active = [automation for automation in automations if enabled(automation)]

            for automation in active:
                try:
                    await _process_automation(
                        worker,
                        state,
                        automation,
                        session_key,
                        state_path,
                    )
                except Exception as error:
                    print(
                        f"[History:{session_key}] erro automação="
                        f"{_automation_id(automation)}: "
                        f"{type(error).__name__} {error}"
                    )

        except Exception as error:
            print(
                f"[History:{session_key}] loop error: "
                f"{type(error).__name__} {error}"
            )

        await asyncio.sleep(15)
