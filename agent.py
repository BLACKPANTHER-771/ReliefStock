"""ReliefStock AI — chat-integrated MVP for Agentverse."""

import asyncio
import json
import os
import re
import shlex
from datetime import datetime, timezone
from uuid import uuid4

import requests
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    TextContent,
    chat_protocol_spec,
)

from allocator import plan_round, run_round
import config
from delivery import confirm_delivery
from dispatch import DispatchPlan, plan_dispatch, apply_dispatch_plan
from models import DeliveryStatus, EventType, Resource, can_transition
from priority import rank_requests
from registry import DEPOTS, all_sites, depots_holding, lookup_site
from rerouting import find_alternative_route, apply_reroute
from routing import RoutePlan
from seed_demo import DEMO_REQUESTS, DEMO_STOCK, seed_demo_safe
from store import CtxBackend, Store
from validation import (
    GROUP_ALIASES,
    RESOURCE_ALIASES,
    parse_resource,
    parse_whole_number,
    validate_request,
)

ASI1_BASE_URL = "https://api.asi1.ai/v1"
ASI1_MODEL = "asi1-mini"
INTENTS = {
    "inventory_query",
    "stock_update",
    "request_create",
    "requests_list",
    "requests_summary",
    "prioritize",
    "allocate",
    "deliveries_list",
    "dispatch",
    "delivery_confirm",
    "reroute",
    "trace",
    "events",
    "help",
    "unknown",
}
FIELDS = {
    "resource",
    "quantity",
    "operation",
    "site",
    "people_affected",
    "severity",
    "vulnerable_groups",
    "delivery_id",
    "received_qty",
    "confirmed_by",
    "lat",
    "lng",
    "site_name",
    "contact_name",
    "contact_phone",
}
_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90,
}


def _numbers_mentioned(text):
    text = re.sub(r"\b(?:DEL|REQ|ALC)-[A-Z0-9]+\b", " ", text, flags=re.I)
    found = set()
    for match in re.finditer(r"\b(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|million)?\b", text, re.I):
        number = float(match.group(1).replace(",", ""))
        scale = (match.group(2) or "").lower()
        if scale in ("k", "thousand"):
            number *= 1000
        elif scale == "million":
            number *= 1000000
        if number.is_integer():
            found.add(int(number))

    tokens = re.findall(r"[a-z]+", text.lower())
    index = 0
    while index < len(tokens):
        total = current = 0
        consumed = False
        while index < len(tokens):
            token = tokens[index]
            if token in _NUMBER_WORDS:
                current += _NUMBER_WORDS[token]
            elif token == "hundred":
                current = max(1, current) * 100
            elif token in ("thousand", "million"):
                scale = 1000 if token == "thousand" else 1000000
                total += max(1, current) * scale
                current = 0
            else:
                break
            consumed = True
            index += 1
        if consumed:
            found.add(total + current)
        else:
            index += 1
    return found


def sanitize_fields(text, fields):
    """Discard extracted values that have no matching evidence in the message."""
    cleaned = dict(fields)
    source = text.lower()
    mentioned_numbers = _numbers_mentioned(text)

    for key in ("quantity", "people_affected", "severity", "received_qty"):
        value = cleaned.get(key)
        if value is None:
            continue
        number, error = parse_whole_number(value, key.replace("_", " "))
        if error or number not in mentioned_numbers:
            cleaned[key] = None

    resource_value = cleaned.get("resource")
    if resource_value is not None:
        resource, error = parse_resource(resource_value)
        normalized_source = source.replace("-", " ").replace("_", " ")
        mentioned = any(
            re.search(r"(?<!\w)%s(?!\w)" % re.escape(alias), normalized_source)
            for alias, candidate in RESOURCE_ALIASES.items()
            if not error and candidate == resource
        )
        if error or not mentioned:
            cleaned["resource"] = None

    if cleaned.get("site") is None and cleaned.get("site_name"):
        cleaned["site"] = cleaned.get("site_name")

    for key in ("site", "delivery_id", "confirmed_by"):
        value = cleaned.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if not value or value.lower() not in source:
            cleaned[key] = None
        else:
            cleaned[key] = value

    operation = cleaned.get("operation")
    if operation is not None:
        operation = str(operation).strip().lower()
        verbs = {
            "add": r"\b(add|increase|raise|boost|restock|receive|received)\b",
            "subtract": r"\b(subtract|decrease|reduce|remove|deduct)\b",
            "set": r"\b(set|have|has|currently|stock is|stocked)\b",
        }
        if operation not in verbs or not re.search(verbs[operation], source):
            cleaned["operation"] = None
        else:
            cleaned["operation"] = operation
    return cleaned


class IntentServiceUnavailable(Exception):
    """ASI1 is missing, unreachable, or returned an unusable response."""

    def __init__(self, reason="invalid response"):
        super().__init__(reason)
        self.reason = reason


class MissingASIAPIKey(IntentServiceUnavailable):
    """The required ASI1 API key is not configured."""


def classify_message(text):
    """Return a validated intent envelope, or raise if ASI1 cannot parse it."""
    environment_loader = getattr(config, "load_local_environment", None)
    if callable(environment_loader):
        try:
            environment_loader()
        except Exception:
            pass
    api_key = os.environ.get(config.ASI1_API_KEY_ENV_VAR)
    if not api_key:
        raise MissingASIAPIKey

    base_url = os.environ.get("ASI1_BASE_URL", ASI1_BASE_URL).rstrip("/")
    prompt = (
        "Classify the user's latest message for a disaster-relief inventory "
        "coordinator. Return only a JSON object with exactly an `intent` string "
        "and a `fields` object. Allowed intents: "
        + ", ".join(sorted(INTENTS))
        + ". Allowed fields: "
        + ", ".join(sorted(FIELDS))
        + ". Extract only facts explicitly stated by the user; use null when a "
        "value is absent or unclear. Never invent IDs, site names, quantities, "
        "severity, people counts, recipients, or other values. `operation` for "
        "stock updates must be `add`, `subtract`, or `set`. Normalize clear "
        "quantity phrases such as 'two thousand' and '2k' to whole numbers. "
        "Classify 'we have N units of RESOURCE' as stock_update with operation "
        "set; 'add/increase stock by N' as stock_update with operation add; "
        "and inventory availability questions as inventory_query. "
        "Classify 'summarize pending relief needs' as requests_summary and "
        "questions asking why needs are prioritized as prioritize. "
        "Do not answer questions or make operational decisions."
    )
    try:
        response = requests.post(
            base_url + "/chat/completions",
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
            },
            json={
                "model": ASI1_MODEL,
                "temperature": 0,
                "max_tokens": 300,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ],
            },
            timeout=12,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise IntentServiceUnavailable
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise IntentServiceUnavailable
        intent = payload.get("intent")
        fields = payload.get("fields")
        if intent not in INTENTS or not isinstance(fields, dict):
            raise IntentServiceUnavailable
        return {
            "intent": intent,
            "fields": sanitize_fields(
                text, {key: value for key, value in fields.items() if key in FIELDS},
            ),
        }
    except requests.Timeout as exc:
        raise IntentServiceUnavailable("request timed out") from exc
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        raise IntentServiceUnavailable("HTTP status %s" % status_code) from exc
    except requests.ConnectionError as exc:
        raise IntentServiceUnavailable("connection failed") from exc
    except (
        requests.RequestException,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
    ) as exc:
        raise IntentServiceUnavailable("invalid response") from exc
    except Exception as exc:
        raise IntentServiceUnavailable("service error: %s" % type(exc).__name__) from exc

agent = Agent()
protocol = Protocol(spec=chat_protocol_spec)


def reply(text, end_session=False):
    content = [TextContent(type="text", text=str(text))]
    if end_session:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=content,
    )


def get_store(ctx):
    """Use Agentverse's persistent storage backend for each incoming message."""
    return Store(CtxBackend(ctx.storage))


def _get_confirmation(store, sender):
    getter = getattr(store, "get_confirmation", None)
    if callable(getter):
        return getter(sender)
    return store.state.setdefault("confirmations", {}).get(sender)


def _set_confirmation(store, sender, action):
    setter = getattr(store, "set_confirmation", None)
    if callable(setter):
        setter(sender, action)
        return
    store.state.setdefault("confirmations", {})[sender] = dict(action)
    store._commit()


def _clear_confirmation(store, sender):
    clearer = getattr(store, "clear_confirmation", None)
    if callable(clearer):
        clearer(sender)
        return
    store.state.setdefault("confirmations", {}).pop(sender, None)
    store._commit()


def parse_fields(parts):
    result = {}
    for part in parts:
        if "=" not in part:
            raise ValueError("Use key=value fields. Example: quantity=100")
        key, value = part.split("=", 1)
        result[key.strip().lower()] = value.strip()
    return result


def help_text():
    return """Lists the supported ReliefStock commands and capabilities.

Hello! I'm ReliefStock AI. You can describe what you need in your own words.

I can check inventory and requests, update stock, register relief requests, rank
urgent needs, allocate available stock, dispatch and track deliveries, confirm
receipt, reroute deliveries, and show delivery traces.

Examples:
- "How much water is left?"
- "Add 2000 units of water" / "Increase our water stock by 2k"
- "Remove 50 water units"
- "Hill Hamlet needs 60 medical kits for 60 people, severity 3"
- "Which village needs help most urgently?"
- "Allocate stock to pending requests"
- "Dispatch delivery DEL-..."
- "The recipient received 100 units for DEL-...; confirmed by Sam"
- "Reroute delivery DEL-..."
- "Trace delivery DEL-..."

I ask you to confirm operational changes before applying them. Reply YES to
confirm or NO to cancel. For legacy structured commands, use:
INVENTORY | SITES | DEPOTS | STOCK resource=water quantity=2000
REQUEST site="Hill Hamlet" resource=water quantity=100 people_affected=50 severity=3
REQUESTS | SUMMARY | ALLOCATE | DELIVERIES | DISPATCH delivery_id=DEL-...
CONFIRM delivery_id=DEL-... received_qty=100 confirmed_by="Recipient name"
REROUTE delivery_id=DEL-... | TRACE delivery_id=DEL-... | EVENTS | DEMO

Resources: water, medical kits, blankets, food packs.
Severity: 1 (low) to 5 (critical).
Unknown sites require coordinator-supplied lat and lng.
Stock changes and allocation rounds affect shared persistent state and require
confirmation.
For a fictional 10-request practice dataset, send DEMO only when the operational
store is empty. DEMO never overwrites existing inventory or records.
Routing may use OSRM; fallback distances/ETAs are estimates, not live traffic.
"""


def _confirmation(store, sender, command, message, **details):
    action = {"command": command}
    action.update(details)
    _set_confirmation(store, sender, action)
    return message + "\nReply YES to confirm or NO to cancel."


def _allocation_snapshot(plan):
    return {
        "round_no": plan.round_no,
        "stock_before": dict(plan.stock_before),
        "rows": [
            [row.request.request_id, row.request.quantity, row.granted, row.status.value]
            for row in plan.rows
        ],
    }


def _format_allocation_preview(plan):
    lines = ["Preview for allocation round %d:" % plan.round_no]
    res_summary = {}
    for row in plan.rows:
        res = row.request.resource.value
        unmet = row.request.quantity - row.granted
        lines.append(
            "%s | %s | requested=%d allocated=%d unmet=%d | %s" % (
                row.request.site_id, res,
                row.request.quantity, row.granted, unmet, row.status.value,
            )
        )
        s = res_summary.setdefault(res, {"req": 0, "alloc": 0})
        s["req"] += row.request.quantity
        s["alloc"] += row.granted

    lines.append("Resource Summary:")
    for res, s in sorted(res_summary.items()):
        unmet = s["req"] - s["alloc"]
        lines.append("- %s: requested %d, allocated %d, unmet %d, remaining stock %d" % (
            res, s["req"], s["alloc"], unmet, plan.stock_after.get(res, 0),
        ))
    return "\n".join(lines)


def _is_greeting(text):
    return bool(re.fullmatch(
        r"(?:hi|hello|hey|howdy|good morning|good afternoon|good evening)"
        r"(?: there)?[!. ]*",
        text.strip().lower(),
    ))


def _local_read_intent(text):
    """Recognize high-confidence local intents that need no model judgment."""
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

    if re.search(r"\b(what can you help me with|what do you do|show help)\b", normalized):
        return {"intent": "help", "fields": {}}

    if re.search(r"\b(how can i|how do i|how to)\b.*\b(request|order|ask for|get supplies)\b", normalized):
        return {"intent": "how_to_request", "fields": {}}

    if re.search(r"\b(how can i|how do i|how to)\b.*\b(donate|add stock|register incoming|incoming inventory|update stock)\b", normalized):
        return {"intent": "how_to_donate", "fields": {}}

    if re.search(r"\b(running low|out of stock|low stock|shortage|shortages|items are low)\b", normalized):
        return {"intent": "inventory_query", "fields": {}}

    if (
        re.search(r"\b(why|how come)\b", normalized)
        and re.search(r"\b(prioriti[sz]ed|priority|urgent)\b", normalized)
    ) or re.search(
        r"\b(which|what|who)\b.*\b(village|request|need|site|location|hamlet)\b.*\b(most urgent|most urgently|urgently|first|highest priority|critical)\b",
        normalized,
    ) or re.search(
        r"\b(who|which village)\b.*\b(needs help|needs assistance|needs relief)\b",
        normalized,
    ) or re.search(r"\b(prioriti[sz]e|rank requests|rank urgent|most urgent)\b", normalized):
        return {"intent": "prioritize", "fields": {}}

    if (
        re.search(r"\b(summarize|summary|overview)\b", normalized)
        and re.search(r"\b(pending|relief|request|need)\b", normalized)
    ) or re.search(r"\bpending relief needs\b", normalized):
        return {"intent": "requests_summary", "fields": {}}

    if re.search(r"\b(show|list|view|display|what are the)\b.*\b(requests|relief requests|pending requests|open requests|all requests)\b", normalized) or re.search(r"\b(all requests|open requests|list requests)\b", normalized):
        return {"intent": "requests_list", "fields": {}}

    if (
        re.search(r"\b(allocate|distribute)\b", normalized)
        and re.search(r"\b(stock|water|supplies|resources|pending|available|fairly)\b", normalized)
    ) or re.search(r"\b(allocate available stock|distribute available water|fair allocation)\b", normalized):
        return {"intent": "allocate", "fields": {}}

    if re.search(r"\b(load|seed|populate)\b.*\b(demo|practice|sample|synthetic)\b", normalized) or normalized in ("demo", "load demo", "load practice dataset"):
        return {"intent": "demo", "fields": {}}

    delivery_match = re.search(r"\b(del-[a-z0-9]+)\b", text, flags=re.I)
    if delivery_match and re.search(r"\b(where|trace|track|status|location|route|find)\b", normalized):
        return {"intent": "trace", "fields": {"delivery_id": delivery_match.group(1).upper()}}
    if re.search(r"\b(where is delivery|trace delivery|track delivery|status of delivery|find delivery)\b", normalized) or normalized.startswith("where is delivery"):
        return {"intent": "trace", "fields": {}}

    if re.search(r"\b(show|list|view|all)\b.*\b(deliveries|shipments)\b", normalized) or re.search(r"\b(list deliveries|all deliveries)\b", normalized):
        return {"intent": "deliveries_list", "fields": {}}

    stock_operation = None
    if re.search(r"\b(add|increase|raise|boost|restock)\b", normalized):
        stock_operation = "add"
    elif re.search(r"\b(remove|subtract|decrease|reduce|deduct)\b", normalized):
        stock_operation = "subtract"
    elif re.search(r"\b(set|we have|our stock is)\b", normalized):
        stock_operation = "set"
    if stock_operation:
        resource = None
        for alias in sorted(RESOURCE_ALIASES, key=len, reverse=True):
            normalized_alias = re.sub(r"[_-]+", " ", alias.lower())
            if re.search(r"(?<!\w)%s(?!\w)" % re.escape(normalized_alias), normalized):
                resource, error = parse_resource(alias)
                if not error:
                    break
                resource = None
        quantities = _numbers_mentioned(text)
        if resource is not None and len(quantities) <= 1:
            return {
                "intent": "stock_update",
                "fields": {
                    "resource": resource.value,
                    "operation": stock_operation,
                    "quantity": next(iter(quantities), None),
                },
            }

    is_stock_change = re.search(
        r"\b(add|increase|raise|boost|restock|set|subtract|decrease|reduce|remove)\b",
        normalized,
    )
    if not is_stock_change and re.search(
        r"\b(inventory|stock|available|left|remaining)\b", normalized,
    ):
        resource = None
        for alias in sorted(RESOURCE_ALIASES, key=len, reverse=True):
            normalized_alias = re.sub(r"[_-]+", " ", alias.lower())
            if re.search(r"(?<!\w)%s(?!\w)" % re.escape(normalized_alias), normalized):
                resource, error = parse_resource(alias)
                if not error:
                    break
                resource = None
        if (
            resource is not None
            and re.search(r"\b(how much|how many|available|left|remaining|stock|inventory|check)\b", normalized)
        ):
            return {
                "intent": "inventory_query",
                "fields": {"resource": resource.value},
            }
        elif re.search(r"\b(check inventory|show inventory|current inventory|what inventory|list inventory|all inventory|inventory status|stock status|what is available)\b", normalized):
            return {"intent": "inventory_query", "fields": {}}

    if re.search(r"\b(needs|requires|asking for|request|relief request)\b", normalized):
        res = None
        matched_alias = None
        for alias in sorted(RESOURCE_ALIASES, key=len, reverse=True):
            norm_alias = re.sub(r"[^a-z0-9]+", " ", alias.lower()).strip()
            if re.search(r"(?<!\w)%s(?!\w)" % re.escape(norm_alias), normalized):
                res = RESOURCE_ALIASES[alias]
                matched_alias = norm_alias
                break
        if res is not None:
            clean_text = re.sub(r"^(?:coordinator|user|operator|me|admin)\s*:\s*", "", text, flags=re.I).strip()
            lat_m = re.search(r"\blat(?:itude)?\s*[:=]\s*(-?\d+(?:\.\d+)?)\b", clean_text, re.I)
            lng_m = re.search(r"\b(?:lng|long(?:itude)?)\s*[:=]\s*(-?\d+(?:\.\d+)?)\b", clean_text, re.I)
            lat = float(lat_m.group(1)) if lat_m else None
            lng = float(lng_m.group(1)) if lng_m else None

            sev_m = re.search(r"\bseverity(?:\s+(?:is|level|:))?\s*(\d+)\b", normalized)
            severity = int(sev_m.group(1)) if sev_m else None
            ppl_m = re.search(r"(\d+)\s*(?:people|residents|persons|individuals|pop|population)\b", normalized)
            people = int(ppl_m.group(1)) if ppl_m else None
            qty_m = re.search(r"(\d+)\s*(?:litres?|liters?|kits?|pieces?|packs?|units?)?\s*(?:of\s+)?" + re.escape(matched_alias), normalized)
            quantity = int(qty_m.group(1)) if qty_m else None
            if quantity is None:
                all_nums = [int(n) for n in re.findall(r"\b\d+\b", normalized)]
                for n in all_nums:
                    if n != severity and n != people:
                        quantity = n
                        break
            sites = all_sites()
            site = None
            for sid, s in sites.items():
                if re.search(r"(?<!\w)%s(?!\w)" % re.escape(sid.lower()), normalized):
                    site = sid
                    break
                if re.search(r"(?<!\w)%s(?!\w)" % re.escape(s.name.lower()), normalized):
                    site = s.name
                    break
            if not site:
                m_pref = re.search(r"^(?:at\s+|for\s+)?(.*?)\s+(?:needs|requires|asking for|requested)\b", clean_text, re.I)
                if m_pref:
                    candidate = m_pref.group(1).strip()
                    if candidate.lower() not in ("what", "who", "which", "how"):
                        site = candidate
            if site:
                site = re.sub(r"\s*(?:at\s+)?(?:lat|lng|long).*$", "", site, flags=re.I).strip()
            groups = []
            for g_alias, g_enum in GROUP_ALIASES.items():
                if re.search(r"(?<!\w)%s(?!\w)" % re.escape(g_alias), normalized):
                    if g_enum.value not in groups:
                        groups.append(g_enum.value)
            fields = {
                "site": site,
                "resource": res.value,
                "quantity": quantity,
                "people_affected": people,
                "severity": severity,
                "vulnerable_groups": groups,
            }
            if lat is not None and lng is not None:
                fields["lat"] = lat
                fields["lng"] = lng
            return {
                "intent": "request_create",
                "fields": fields,
            }

    disp_m = re.search(r"\bdispatch\b.*\b(del-[a-z0-9-]+)\b", normalized)
    if disp_m:
        return {"intent": "dispatch", "fields": {"delivery_id": disp_m.group(1).upper()}}

    reroute_m = re.search(r"\breroute\b.*\b(del-[a-z0-9-]+)\b", normalized)
    if reroute_m:
        return {"intent": "reroute", "fields": {"delivery_id": reroute_m.group(1).upper()}}

    confirm_m = re.search(r"\b(received|confirmed|confirm delivery)\b.*\b(del-[a-z0-9-]+)\b", normalized)
    if confirm_m:
        del_id = confirm_m.group(2).upper()
        qty_m = re.search(r"(\d+)\s*(?:units?|litres?|kits?|pieces?|packs?)?", normalized)
        by_m = re.search(r"\bconfirmed by\s+([a-z\s]+)", text, re.I)
        fields = {"delivery_id": del_id}
        if qty_m:
            fields["received_qty"] = int(qty_m.group(1))
        if by_m:
            fields["confirmed_by"] = by_m.group(1).strip().strip(";.,")
        return {"intent": "delivery_confirm", "fields": fields}

    return None


def _intent_command(intent, fields):
    commands = {
        "inventory_query": ("INVENTORY", ("resource",)),
        "stock_update": ("ADJUST", ("resource", "operation", "quantity")),
        "request_create": (
            "REQUEST",
            ("site", "resource", "quantity", "people_affected", "severity",
             "vulnerable_groups", "lat", "lng", "site_name", "contact_name", "contact_phone"),
        ),
        "requests_list": ("REQUESTS", ()),
        "requests_summary": ("SUMMARY", ()),
        "prioritize": ("PRIORITIZE", ()),
        "allocate": ("ALLOCATE", ()),
        "deliveries_list": ("DELIVERIES", ()),
        "dispatch": ("DISPATCH", ("delivery_id",)),
        "delivery_confirm": (
            "CONFIRM",
            ("delivery_id", "received_qty", "confirmed_by"),
        ),
        "reroute": ("REROUTE", ("delivery_id",)),
        "trace": ("TRACE", ("delivery_id",)),
        "events": ("EVENTS", ()),
        "demo": ("DEMO", ()),
    }
    selected = commands.get(intent)
    if selected is None:
        return None
    name, keys = selected
    parts = [name]
    for key in keys:
        value = fields.get(key)
        if (value is None or value == "") and key == "site" and fields.get("site_name"):
            value = fields.get("site_name")
        if value is None or value == "":
            continue
        if key == "vulnerable_groups" and isinstance(value, list):
            value = ",".join(str(item) for item in value)
        parts.append("%s=%s" % (key, shlex.quote(str(value))))
    return " ".join(parts)


def handle_command(ctx, text, sender="local", confirmed=False, confirmation=None):
    parts = shlex.split(text)
    if not parts:
        return "Send HELP to see available commands."

    command = parts[0].upper()
    args = parts[1:]
    store = get_store(ctx)

    if command in ("HELP", "START"):
        return help_text()

    if command == "DEMO":
        if not confirmed:
            missing_stock = []
            for resource, min_qty in DEMO_STOCK.items():
                current_qty = store.get_stock(resource.value)
                if current_qty < min_qty:
                    missing_stock.append("%s (%d -> %d)" % (resource.value, current_qty, min_qty))
            stock_note = (
                " Missing demo stock will be added: %s." % ", ".join(missing_stock)
                if missing_stock else " Existing inventory will be preserved."
            )
            return _confirmation(
                store,
                sender,
                text,
                "Load the fictional 10-request practice dataset?%s"
                " Any higher existing inventory will be preserved."
                % stock_note,
            )
        open_requests = store.open_requests()
        open_demo_keys = {(r.site_id, r.resource.value) for r in open_requests}
        all_demo_keys = {(raw["site"], raw["resource"]) for _, _, raw in DEMO_REQUESTS}
        if all_demo_keys.issubset(open_demo_keys):
            return (
                "Demo practice dataset is already loaded and active (%d pending requests). "
                "No duplicate records were created. Inventory: %s. "
                "Use SUMMARY, REQUESTS, INVENTORY, or PRIORITIZE to inspect it."
                % (
                    len(open_requests),
                    ", ".join(
                        "%s=%d" % (resource, quantity)
                        for resource, quantity in sorted(store.get_inventory().items())
                    ),
                )
            )

        result = seed_demo_safe(store)
        verified_open = store.open_requests()
        return (
            "Fictional practice dataset loaded: %d pending requests across 9 "
            "registered demo sites. Inventory: %s. This data is synthetic; "
            "use SUMMARY, REQUESTS, INVENTORY, or PRIORITIZE to inspect it."
            % (
                len(verified_open),
                ", ".join(
                    "%s=%d" % (resource, quantity)
                    for resource, quantity in sorted(store.get_inventory().items())
                ),
            )
        )

    if command == "INVENTORY":
        inventory = store.get_inventory()
        open_reqs = store.open_requests()
        demand = {}
        for r in open_reqs:
            demand[r.resource.value] = demand.get(r.resource.value, 0) + r.quantity

        if args:
            fields = parse_fields(args)
            resource, error = parse_resource(fields.get("resource", ""))
            if error:
                return error
            current = store.get_stock(resource.value)
            needed = demand.get(resource.value, 0)
            status_line = "Shows available resources and stock quantities: Current %s inventory: %d %s." % (
                resource.value, current, resource.unit,
            )
            if needed > current:
                status_line += " Shortage: %d %s needed across pending requests." % (
                    needed - current, resource.unit,
                )
            elif needed > 0:
                status_line += " Pending demand: %d %s." % (needed, resource.unit)
            else:
                status_line += " No pending demand."
            return status_line

        units = {
            "water": "litres",
            "medical_kits": "kits",
            "blankets": "pieces",
            "food_packs": "packs",
        }
        lines = ["Shows available resources and stock quantities:\nCurrent inventory:"]
        for name, quantity in inventory.items():
            needed = demand.get(name, 0)
            unit = units.get(name, "units")
            line = "- %s: %d %s" % (name.replace("_", " "), quantity, unit)
            if needed > quantity:
                line += " (SHORTAGE: %d %s requested, need %d more)" % (needed, unit, needed - quantity)
            elif needed > 0:
                line += " (%d %s requested)" % (needed, unit)
            lines.append(line)
        return "\n".join(lines)

    if command == "SITES":
        from registry import all_sites
        sites = all_sites(store.extra_sites())
        return "Lists registered relief sites:\n" + "\n".join(
            "%s — %s (%.4f, %.4f)" %
            (site.site_id, site.name, site.lat, site.lng)
            for site in sites.values()
        )

    if command == "DEPOTS":
        return "Lists available supply depots:\n" + "\n".join(
            "%s — %s; resources: %s" %
            (depot.depot_id, depot.name, ", ".join(depot.resources))
            for depot in DEPOTS.values()
        )

    if command == "STOCK":
        fields = parse_fields(args)
        resource, error = parse_resource(fields.get("resource", ""))
        if error:
            return error
        quantity, error = parse_whole_number(fields.get("quantity", ""), "Stock quantity")
        if error:
            return error
        if quantity < 0:
            return "Stock quantity must be zero or greater."
        store.set_stock(resource, quantity, source="chat coordinator command")
        return "Updates water inventory and confirms the result: %s = %d %s." % (
            resource.value, quantity, resource.unit,
        )

    if command == "ADJUST":
        fields = parse_fields(args)
        if not fields.get("resource"):
            return "Which resource should I update: water, medical kits, blankets, or food packs?"
        resource, error = parse_resource(fields.get("resource", ""))
        if error:
            return error
        operation = fields.get("operation", "").lower()
        operation = {
            "increase": "add",
            "decrease": "subtract",
            "replace": "set",
            "update": "set",
        }.get(operation, operation)
        if operation not in ("add", "subtract", "set"):
            return "Should I add to, subtract from, or set the current stock?"
        if fields.get("quantity") in (None, ""):
            return "How many units should I apply to the stock update?"
        quantity, error = parse_whole_number(fields.get("quantity", ""), "Stock change")
        if error:
            return error
        if quantity < 0 or (quantity == 0 and operation != "set"):
            return "Stock change must be greater than zero."
        current = store.get_stock(resource.value)
        delta = quantity if operation == "add" else -quantity
        updated = quantity if operation == "set" else current + delta
        if updated < 0:
            return "Stock cannot go below zero (current %d, change %d)." % (current, delta)
        if not confirmed:
            prompt = (
                "Set %s stock from %d to %d %s?" % (
                    resource.value, current, updated, resource.unit,
                )
                if operation == "set"
                else "%s %d %s of %s stock (%d -> %d)?" % (
                    operation.capitalize(), quantity, resource.unit,
                    resource.value, current, updated,
                )
            )
            return _confirmation(
                store, sender, text, prompt,
                stock_resource=resource.value,
                stock_before=current,
            )
        if operation == "set":
            store.set_stock(resource, quantity, source="chat coordinator natural-language update")
        else:
            store.adjust_stock(resource, delta, "chat coordinator natural-language update")
        return "Stock updated: %s = %d %s." % (resource.value, updated, resource.unit)

    if command == "REQUEST":
        fields = parse_fields(args)
        result, new_site = validate_request(
            fields,
            extra_sites=store.extra_sites(),
            open_requests=store.open_requests(),
        )
        if not result.ok:
            store.log_rejection(str(fields), result.errors)
            return "Request rejected:\n- " + "\n- ".join(result.errors)
        if not confirmed:
            return _confirmation(
                store, sender, text,
                "Submit request for %d %s at %s (severity %d, %d people affected)?" % (
                    result.request.quantity, result.request.resource.value,
                    result.request.site_id, result.request.severity,
                    result.request.people_affected,
                ),
            )
        if new_site is not None:
            store.add_site(new_site)
        request = result.request
        store.add_request(request)
        return (
            "Request accepted.\nID: %s\nSite: %s\nResource: %s\nQuantity: %d\n"
            "People affected: %d\nSeverity: %d\nStatus: PENDING\n"
            "No stock has been allocated yet. The coordinator must run ALLOCATE."
            % (request.request_id, request.site_id, request.resource.value,
               request.quantity, request.people_affected, request.severity)
        )

    if command == "REQUESTS":
        requests = store.all_requests()
        if not requests:
            return (
                "Lists submitted requests and their statuses: No relief requests are recorded in this agent's persistent store yet. "
                "Tell me the site, resource, quantity, people affected, and severity "
                "to submit a request; I will validate it and ask for confirmation."
            )
        return "Lists submitted requests and their statuses:\n" + "\n".join(
            "%s | %s | %s | requested=%d allocated=%d | %s" % (
                r.request_id, r.site_id, r.resource.value,
                r.quantity, r.allocated_qty, r.status.value
            ) for r in requests
        )

    if command == "SUMMARY":
        pending = store.open_requests()
        if not pending:
            return (
                "There are no pending relief requests in this agent's persistent store. "
                "No needs have been submitted yet, so there is nothing to summarize. "
                "To add a real need, provide a registered site, resource, quantity, "
                "people affected, and severity; I will validate it and ask for confirmation."
            )
        totals = {}
        for request in pending:
            key = (request.site_id, request.resource.value)
            totals[key] = totals.get(key, 0) + request.quantity
        lines = [
            "Pending relief needs: %d request%s across %d site%s." % (
                len(pending),
                "" if len(pending) == 1 else "s",
                len({request.site_id for request in pending}),
                "" if len({request.site_id for request in pending}) == 1 else "s",
            )
        ]
        lines.extend(
            "%s | %s | requested=%d" % (site_id, resource, quantity)
            for (site_id, resource), quantity in sorted(totals.items())
        )
        return "\n".join(lines)

    if command == "ALLOCATE":
        plan = plan_round(store)
        if not plan.rows:
            return "Runs allocation and reports the resulting quantities: No pending requests to allocate."
        if not confirmed:
            preview = (
                "Runs allocation and reports the resulting quantities: Preview for allocation round %d.\n"
                % (store.current_round() + 1)
                + _format_allocation_preview(plan)
            )
            return _confirmation(
                store, sender, text, preview,
                allocation_snapshot=_allocation_snapshot(plan),
            )
        plan = run_round(store)
        if not plan.rows:
            return "Runs allocation and reports the resulting quantities: The allocation round produced no rows."
        lines = ["Runs allocation and reports the resulting quantities: Allocation round %d completed." % plan.round_no]
        delivery_ids = {
            allocation.request_id: allocation.delivery_id
            for allocation in store.all_allocations()
        }
        res_summary = {}
        for row in plan.rows:
            res = row.request.resource.value
            unmet = row.request.quantity - row.granted
            lines.append(
                "%s | %s | requested=%d allocated=%d unmet=%d | %s%s" % (
                    row.request.site_id, res,
                    row.request.quantity, row.granted, unmet, row.status.value,
                    (" — " + row.reason) if row.reason else "",
                )
            )
            s = res_summary.setdefault(res, {"req": 0, "alloc": 0})
            s["req"] += row.request.quantity
            s["alloc"] += row.granted
            delivery_id = delivery_ids.get(row.request.request_id)
            if delivery_id:
                lines.append("  Delivery ID: %s" % delivery_id)
        lines.append("\nResource Breakdown:")
        for res, s in sorted(res_summary.items()):
            unmet = s["req"] - s["alloc"]
            lines.append("- %s: requested %d, allocated %d, unmet %d, remaining stock %d" % (
                res, s["req"], s["alloc"], unmet, store.get_stock(res),
            ))
        lines.append(
            "Remaining stock: " + ", ".join(
                "%s=%d" % (resource, quantity)
                for resource, quantity in store.get_inventory().items()
            )
        )
        lines.append(
            "\nNote: Deliveries have been registered in the ledger. Supplies have not yet been physically dispatched or delivered."
        )
        return "\n".join(lines)

    if command == "PRIORITIZE":
        ranked = rank_requests(store.open_requests(), store.prior_allocation())
        if not ranked:
            return (
                "There are no pending requests to prioritize in this agent's persistent "
                "store. Submit a validated relief request first; rankings use the stored "
                "severity, affected population, vulnerable groups, waiting time, and "
                "prior-allocation history. To register a request, tell me the village, resource, "
                "quantity, people affected, and severity (1-5)."
            )
        top_req, top_score = ranked[0]
        site_match = lookup_site(top_req.site_id, store.extra_sites())[0]
        top_site_name = site_match.name if site_match else top_req.site_id
        return "Most urgent pending requests (Top: %s):\n" % top_site_name + "\n".join(
            "%d. %s (%s) | %s | %s | requested=%d | %s" % (
                score.rank,
                (lookup_site(request.site_id, store.extra_sites())[0].name if lookup_site(request.site_id, store.extra_sites())[0] else request.site_id),
                request.site_id, request.resource.value,
                request.request_id, request.quantity, score.explain(),
            )
            for request, score in ranked
        )

    if command == "DELIVERIES":
        try:
            deliveries = store.all_deliveries()
        except Exception:
            deliveries = []
        if not deliveries:
            return (
                "Lists deliveries and their statuses:\n"
                "No active deliveries are currently in transit.\n"
                "Format: Delivery ID | Site ID | Resource | Sent / Received | Status (ALLOCATED, DISPATCHED, DELIVERED)"
            )
        lines = ["Lists deliveries and their statuses:"]
        for d in deliveries:
            try:
                res_val = d.resource.value if hasattr(d.resource, "value") else str(getattr(d, "resource", "water"))
                stat_val = d.status.value if hasattr(d.status, "value") else str(getattr(d, "status", "ALLOCATED"))
                sent = int(getattr(d, "sent_qty", 0) or 0)
                recv = int(getattr(d, "received_qty", 0) or 0)
                did = getattr(d, "delivery_id", "DEL-UNKNOWN")
                sid = getattr(d, "site_id", "UNKNOWN")
                lines.append("%s | site=%s | %s | sent=%d received=%d | %s" % (
                    did, sid, res_val, sent, recv, stat_val
                ))
            except Exception:
                continue
        if len(lines) == 1:
            lines.append(
                "No active deliveries are currently in transit.\n"
                "Format: Delivery ID | Site ID | Resource | Sent / Received | Status (ALLOCATED, DISPATCHED, DELIVERED)"
            )
        return "\n".join(lines)

    if command == "DISPATCH":
        fields = parse_fields(args)
        delivery_id = fields.get("delivery_id", "")
        if not delivery_id:
            return "Which delivery ID should I dispatch? Use DELIVERIES to see available IDs."
        delivery = store.get_delivery(delivery_id)
        if delivery is None:
            return "Delivery not found. Use DELIVERIES to see valid IDs."
        if not can_transition(delivery.status, DeliveryStatus.DISPATCHED):
            return "Cannot dispatch delivery from status %s." % delivery.status.value
        allocation = store.get_allocation(delivery.allocation_id)
        if allocation is None:
            return "Associated allocation not found."
        plan = (
            DispatchPlan(**confirmation["dispatch_plan"])
            if confirmation and confirmation.get("dispatch_plan")
            else plan_dispatch(allocation)
        )
        if plan is None:
            return "No eligible depot holds this resource."
        if not confirmed:
            return _confirmation(
                store, sender, text,
                "Dispatch %d %s for delivery %s from depot %s? Estimated travel time: %d minutes." % (
                    plan.quantity, plan.resource, delivery_id,
                    plan.depot_id, plan.eta_minutes,
                ),
                dispatch_plan=plan.to_dict(),
            )
        apply_dispatch_plan(allocation, delivery, plan)
        delivery.status = DeliveryStatus.DISPATCHED
        store.update_delivery(delivery)
        store.log(
            EventType.DISPATCHED,
            "Delivery %s dispatched from %s." % (delivery_id, plan.depot_id),
            delivery_id=delivery_id,
            allocation_id=delivery.allocation_id,
            site_id=delivery.site_id,
            data=plan.to_dict(),
        )
        return (
            "Delivery dispatched.\nDelivery ID: %s\nDepot: %s\nDistance: %.2f km\n"
            "Estimated travel time: %d minutes\nRoute: %s"
            % (delivery_id, plan.depot_id, plan.distance_km,
               plan.eta_minutes, plan.route)
        )

    if command == "CONFIRM":
        fields = parse_fields(args)
        delivery_id = fields.get("delivery_id", "")
        if not delivery_id:
            return "Which delivery ID should I confirm? Use DELIVERIES to see available IDs."
        delivery = store.get_delivery(delivery_id)
        if delivery is None:
            return "Delivery not found. Use DELIVERIES to see valid IDs."
        received_text = fields.get("received_qty")
        if received_text is None:
            return "Provide received_qty. Example: CONFIRM delivery_id=DEL-... received_qty=100"
        received, error = parse_whole_number(received_text, "Received quantity")
        if error:
            return error
        if received < 0:
            return "Received quantity cannot be negative."
        confirmed_by = fields.get("confirmed_by", "chat coordinator")
        if not confirmed:
            return _confirmation(
                store, sender, text,
                "Record receipt of %d of %d %s for delivery %s, confirmed by %s?" % (
                    received, delivery.sent_qty, delivery.resource.value,
                    delivery_id, confirmed_by,
                ),
            )
        try:
            result = confirm_delivery(
                delivery, received, confirmed_by,
            )
        except ValueError as exc:
            return str(exc)
        store.update_delivery(delivery)
        if result["mismatch"]:
            store.log(
                EventType.DELIVERY_MISMATCH, result["message"],
                delivery_id=delivery_id,
                allocation_id=delivery.allocation_id,
                site_id=delivery.site_id,
                data={"sent": result["expected_qty"], "received": result["received_qty"]},
            )
            return result["message"] + "\nDelivery remains open for investigation."
        store.log(
            EventType.DELIVERED, result["message"],
            delivery_id=delivery_id,
            allocation_id=delivery.allocation_id,
            site_id=delivery.site_id,
            data={"received": received},
        )
        return result["message"]

    if command == "REROUTE":
        fields = parse_fields(args)
        delivery_id = fields.get("delivery_id", "")
        if not delivery_id:
            return "Which delivery ID should I reroute? Use DELIVERIES to see available IDs."
        delivery = store.get_delivery(delivery_id)
        if delivery is None:
            return "Delivery not found. Use DELIVERIES to see valid IDs."
        site = lookup_site(delivery.site_id, extra_sites=store.extra_sites())
        if site is None:
            return "Delivery destination site could not be found."
        route_plan = (
            RoutePlan(**confirmation["route_plan"])
            if confirmation and confirmation.get("route_plan")
            else find_alternative_route(delivery, site)
        )
        if route_plan is None:
            return "No alternative eligible depot is available."
        if not confirmed:
            return _confirmation(
                store, sender, text,
                "Reroute delivery %s via depot %s? Estimated distance: %.2f km; ETA: %d minutes." % (
                    delivery_id, route_plan.depot_id, route_plan.distance_km,
                    route_plan.eta_minutes,
                ),
                route_plan=route_plan.__dict__,
            )
        try:
            apply_reroute(delivery, route_plan)
        except ValueError as exc:
            return str(exc)
        store.update_delivery(delivery)
        store.log(
            EventType.ROUTE_CHANGED,
            "Delivery %s rerouted through %s." % (delivery_id, route_plan.depot_id),
            delivery_id=delivery_id,
            allocation_id=delivery.allocation_id,
            site_id=delivery.site_id,
            data=route_plan.to_dict(),
        )
        return (
            "Delivery rerouted.\nDelivery ID: %s\nNew depot: %s\n"
            "Estimated distance: %.2f km\nEstimated travel time: %d minutes"
            % (delivery_id, route_plan.depot_id,
               route_plan.distance_km, route_plan.eta_minutes)
        )

    if command == "TRACE":
        fields = parse_fields(args)
        delivery_id = fields.get("delivery_id", "").strip().rstrip("-")
        if not delivery_id or delivery_id.upper() == "DEL":
            deliveries = store.all_deliveries()
            if deliveries:
                recent_ids = ", ".join(d.delivery_id for d in deliveries[-3:])
                return (
                    "Which delivery ID should I trace? Please specify a complete ID. "
                    "Available deliveries: %s (or send DELIVERIES to list all)." % recent_ids
                )
            return "Which delivery ID should I trace? Please specify a complete ID (e.g. DEL-...). No deliveries have been recorded yet."
        delivery = store.get_delivery(delivery_id)
        events = store.trace(delivery_id)
        if not delivery and not events:
            deliveries = store.all_deliveries()
            avail = (" Available: %s" % ", ".join(d.delivery_id for d in deliveries[-3:])) if deliveries else ""
            return "No delivery or trace found for ID '%s'.%s" % (delivery_id, avail)
        lines = []
        if delivery:
            site_obj = lookup_site(delivery.site_id, store.extra_sites())[0]
            site_name = site_obj.name if site_obj else delivery.site_id
            lines.append("Delivery %s: Status %s" % (delivery.delivery_id, delivery.status.value))
            lines.append("Destination: %s (%s) | Resource: %s | Sent: %d, Received: %d" % (
                site_name, delivery.site_id, delivery.resource.value, delivery.sent_qty, delivery.received_qty,
            ))
            if delivery.depot_id:
                lines.append("Depot: %s" % delivery.depot_id)
            if delivery.distance_km > 0 or delivery.eta_minutes > 0:
                lines.append("Estimated distance: %.2f km; ETA: %d minutes" % (delivery.distance_km, delivery.eta_minutes))
            if delivery.route:
                lines.append("Route: %s" % delivery.route)
        lines.append("Delivery trace:")
        if events:
            for event in events:
                lines.append("%s | %s" % (event.timestamp, event.message))
        else:
            lines.append("No event history recorded yet.")
        return "\n".join(lines)

    if command == "EVENTS":
        events = store.events(limit=20)
        if not events:
            return "No events have been logged."
        return "Recent events:\n" + "\n".join(
            "%s | %s | %s" % (event.timestamp, event.event_type.value, event.message)
            for event in events
        )

    return "Unknown command. Send HELP for supported commands."


_STRUCTURED_COMMANDS = {
    "HELP", "START", "INVENTORY", "SITES", "DEPOTS", "STOCK", "REQUEST",
    "ADJUST", "REQUESTS", "SUMMARY", "PRIORITIZE", "ALLOCATE", "DELIVERIES", "DISPATCH",
    "DEMO",
    "CONFIRM", "REROUTE", "TRACE", "EVENTS",
}
_YES = re.compile(r"^(?:yes|y|confirm|confirmed|proceed|go ahead)(?:[!. ]*)$", re.I)
_NO = re.compile(r"^(?:no|n|cancel|stop)(?:[!. ]*)$", re.I)


def _handle_confirmation(ctx, sender, text):
    store = get_store(ctx)
    pending = _get_confirmation(store, sender)
    if not pending:
        if _YES.fullmatch(text.strip()):
            return "There is no pending action to confirm."
        if _NO.fullmatch(text.strip()):
            return "There is no pending action to cancel."
        return None
    if _NO.fullmatch(text.strip()):
        _clear_confirmation(store, sender)
        return "Pending action cancelled."
    if not _YES.fullmatch(text.strip()):
        if _is_structured_command(text) or _is_greeting(text):
            _clear_confirmation(store, sender)
            return None
        return "There is a pending action. Reply YES to confirm it or NO to cancel it."

    if "stock_resource" in pending:
        current = store.get_stock(pending["stock_resource"])
        if current != pending["stock_before"]:
            _clear_confirmation(store, sender)
            return handle_command(ctx, pending["command"], sender)

    if "allocation_snapshot" in pending:
        current_plan = plan_round(store)
        if _allocation_snapshot(current_plan) != pending["allocation_snapshot"]:
            pending["allocation_snapshot"] = _allocation_snapshot(current_plan)
            _set_confirmation(store, sender, pending)
            return (
                "Requests or inventory changed since the previous preview.\n"
                + _format_allocation_preview(current_plan)
                + "\nPlease review and confirm this updated plan."
            )

    _clear_confirmation(store, sender)
    return handle_command(
        ctx, pending["command"], sender, confirmed=True, confirmation=pending,
    )


def _is_structured_command(text):
    first = text.strip().split(None, 1)
    return bool(first and first[0].upper() in _STRUCTURED_COMMANDS)


@protocol.on_message(ChatMessage)
async def handle_message(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc),
            acknowledged_msg_id=msg.msg_id,
        ),
    )
    text = msg.text()
    if not text:
        return
    text = text.strip()
    if _is_greeting(text) or text.split(None, 1)[0].upper() in ("HELP", "START"):
        result = help_text()
    else:
        stage = "routing message"
        try:
            local_envelope = (
                None if _is_structured_command(text) else _local_read_intent(text)
            )
            read_only_intents = {
                "help",
                "how_to_request",
                "how_to_donate",
                "inventory_query",
                "prioritize",
                "requests_summary",
                "requests_list",
                "deliveries_list",
                "trace",
                "events",
            }
            if local_envelope and local_envelope["intent"] in read_only_intents:
                result = None
                envelope = local_envelope
                stage = "read-only question"
            else:
                stage = "pending confirmation check"
                result = _handle_confirmation(ctx, sender, text)
                envelope = None

            if result is None:
                if _is_structured_command(text):
                    stage = "structured command"
                    result = handle_command(
                        ctx, text, sender,
                        confirmed=(text.strip().upper() == "ALLOCATE"),
                    )
                else:
                    if envelope is None:
                        envelope = local_envelope
                    if envelope is None:
                        stage = "ASI1 intent classification"
                        try:
                            envelope = await asyncio.to_thread(classify_message, text)
                        except Exception as ai_exc:
                            fallback = _local_read_intent(text)
                            if fallback is not None:
                                envelope = fallback
                                ctx.logger.warning("ASI1 failed (%s); using local deterministic fallback.", ai_exc)
                            else:
                                raise
                    intent = envelope["intent"]
                    if intent == "help":
                        result = help_text()
                    elif intent == "how_to_request":
                        result = (
                            "To submit a relief request for a site or village, tell me:\n"
                            "1. Site name (e.g., 'Hill Hamlet' or specify lat and lng for new locations)\n"
                            "2. Resource needed (water, medical kits, blankets, food packs)\n"
                            "3. Quantity needed and unit\n"
                            "4. People affected count\n"
                            "5. Severity level from 1 (low) to 5 (critical)\n\n"
                            "Example: 'Hill Hamlet needs 60 medical kits for 60 people, severity 3'\n"
                            "Or use structured command: REQUEST site=\"Hill Hamlet\" resource=medical_kits quantity=60 people_affected=60 severity=3"
                        )
                    elif intent == "how_to_donate":
                        result = (
                            "To register incoming inventory or update depot stock:\n"
                            "- Natural language: 'Add 2000 litres of water' or 'Set blankets stock to 500'\n"
                            "- Structured command: 'STOCK resource=water quantity=2000' or 'ADJUST resource=water operation=add quantity=2000'\n\n"
                            "I will display the before and after stock levels and request confirmation before applying the update."
                        )
                    elif intent == "unknown":
                        result = (
                            "I couldn't identify that request. You can ask about inventory, "
                            "stock, relief requests, priority, allocation, dispatch, delivery, "
                            "rerouting, or tracing. Send HELP for examples."
                        )
                    else:
                        stage = (
                            "read-only request summary"
                            if intent in ("requests_summary", "requests_list", "deliveries_list", "trace", "events")
                            else "operational command"
                        )
                        command = _intent_command(intent, envelope["fields"])
                        result = (
                            handle_command(ctx, command, sender)
                            if command
                            else "I couldn't identify that request. Send HELP for examples."
                        )
        except MissingASIAPIKey:
            ctx.logger.info("ASI1 unavailable: ASI1_API_KEY is not configured")
            result = (
                "Natural-language support is not configured because ASI1_API_KEY is missing. "
                "Set it in your local .env or Agentverse secrets, or use the structured "
                "commands; send HELP for examples."
            )
        except IntentServiceUnavailable as exc:
            ctx.logger.info("ASI1 intent classification failed: %s" % exc.reason)
            result = (
                "Natural-language interpretation is unavailable right now (%s). "
                "Check the Agentverse ASI1_API_KEY secret and ASI1 service status, "
                "or use structured commands. Read-only inventory, priority, and "
                "pending-needs questions are available without ASI." % exc.reason
            )
        except Exception as exc:
            detail = ""
            if isinstance(exc, (AttributeError, NameError)) and getattr(exc, "name", None):
                detail = " (missing: %s)" % exc.name
            ctx.logger.error(
                "ReliefStock handling failed during %s (%s)%s." % (
                    stage, type(exc).__name__, detail,
                )
            )
            if isinstance(exc, ValueError):
                result = str(exc)
            else:
                result = (
                    "The request failed during %s (%s)%s. No success is being reported; "
                    "check the agent logs and try again." % (
                        stage, type(exc).__name__, detail,
                    )
                )
    await ctx.send(sender, reply(result))


@protocol.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(protocol, publish_manifest=True)

if __name__ == "__main__":
    agent.run()
