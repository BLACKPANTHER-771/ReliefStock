import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from agent import (
    IntentServiceUnavailable,
    MissingASIAPIKey,
    classify_message,
    handle_message,
    help_text,
    sanitize_fields,
)
from allocator import run_round
import config
from dispatch import DispatchPlan
from models import Allocation, Delivery, DeliveryStatus, Resource, ResourceRequest
from routing import RoutePlan
from store import CtxBackend, Store
from uagents_core.contrib.protocols.chat import ChatMessage, TextContent


class MemoryStorage:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value


class TestLogger:
    def __init__(self):
        self.errors = []

    def info(self, message):
        pass

    def exception(self, message):
        pass

    def error(self, message):
        self.errors.append(message)


class TestContext:
    def __init__(self):
        self.storage = MemoryStorage()
        self.logger = TestLogger()
        self.sent = []

    async def send(self, recipient, message):
        self.sent.append((recipient, message))


class ConversationTests(TestCase):
    def setUp(self):
        self.ctx = TestContext()
        self.sender = "test-user"

    def say(self, text):
        message = ChatMessage(
            timestamp=datetime.now(timezone.utc),
            msg_id=uuid4(),
            content=[TextContent(type="text", text=text)],
        )
        asyncio.run(handle_message(self.ctx, self.sender, message))
        return self.ctx.sent[-1][1].text()

    def classify_as(self, intent, fields=None):
        return patch(
            "agent.classify_message",
            return_value={"intent": intent, "fields": fields or {}},
        )

    def store(self):
        return Store(CtxBackend(self.ctx.storage))

    def test_greetings_and_help_are_local_and_read_only(self):
        with patch("agent.classify_message") as classifier:
            greeting = self.say("hi")
            help_response = self.say("HELP")
        self.assertIn("describe what you need in your own words", greeting)
        self.assertEqual(help_response, help_text())
        classifier.assert_not_called()
        self.assertEqual(self.ctx.storage.data, {})

    def test_natural_inventory_query_reads_store_without_mutation(self):
        store = self.store()
        store.set_stock(Resource.WATER, 37)
        state_before = self.store().state.copy()
        with self.classify_as("inventory_query", {"resource": "water"}):
            response = self.say("How much water is left?")
        with self.classify_as("inventory_query", {"resource": "water"}):
            alternate = self.say("What is our water stock?")
        self.assertIn("37 litres", response)
        self.assertIn("37 litres", alternate)
        self.assertEqual(self.store().get_stock("water"), 37)
        self.assertEqual(self.store().events(), Store(CtxBackend(self.ctx.storage)).events())
        self.assertEqual(state_before["inventory"], self.store().state["inventory"])

    def test_natural_stock_phrasings_require_confirmation_and_use_store(self):
        examples = [
            ("Add 2000 units of water", {"operation": "add", "quantity": 2000}),
            ("We have two thousand water units", {"operation": "set", "quantity": 2000}),
            ("Increase our water stock by 2k", {"operation": "add", "quantity": 2000}),
        ]
        for phrase, fields in examples:
            with self.subTest(phrase=phrase):
                ctx = TestContext()
                self.ctx = ctx
                store = self.store()
                store.set_stock(Resource.WATER, 10)
                with self.classify_as(
                    "stock_update", {"resource": "water", **fields}
                ):
                    prompt = self.say(phrase)
                self.assertIn("Reply YES to confirm", prompt)
                self.assertEqual(self.store().get_stock("water"), 10)
                if fields["operation"] == "set":
                    real_set = Store.set_stock
                    with patch.object(
                        Store, "set_stock", autospec=True, side_effect=real_set,
                    ) as set_stock:
                        response = self.say("yes")
                    set_stock.assert_called_once()
                else:
                    real_adjust = Store.adjust_stock
                    with patch.object(
                        Store, "adjust_stock", autospec=True, side_effect=real_adjust,
                    ) as adjust:
                        response = self.say("yes")
                    adjust.assert_called_once()
                self.assertIn("Stock updated: water", response)
                expected = 2010 if fields["operation"] == "add" else 2000
                self.assertEqual(self.store().get_stock("water"), expected)

    def test_local_natural_stock_add_and_remove_work_without_asi(self):
        self.store().set_stock(Resource.WATER, 100)
        with patch("agent.classify_message") as classifier:
            add_preview = self.say("Add 25 units of water")
            self.assertIn("Reply YES to confirm", add_preview)
            self.assertIn("100 -> 125", add_preview)
            added = self.say("YES")
            self.assertIn("water = 125 litres", added)

            remove_preview = self.say("Remove 40 water units")
            self.assertIn("125 -> 85", remove_preview)
            removed = self.say("YES")
            self.assertIn("water = 85 litres", removed)

        classifier.assert_not_called()
        self.assertEqual(self.store().get_stock("water"), 85)

    def test_confirmation_fallback_supports_older_store_without_confirmation_methods(self):
        store = self.store()
        store.set_stock(Resource.WATER, 20)
        with patch.object(Store, "get_confirmation", None), \
                patch.object(Store, "set_confirmation", None), \
                patch.object(Store, "clear_confirmation", None):
            with self.classify_as(
                "stock_update",
                {"resource": "water", "operation": "add", "quantity": 5},
            ):
                preview = self.say("Add 5 units of water")
            self.assertIn("Reply YES to confirm", preview)
            self.assertEqual(self.store().get_stock("water"), 20)

            response = self.say("YES")

        self.assertIn("Stock updated: water = 25 litres", response)
        self.assertEqual(self.store().get_stock("water"), 25)
        self.assertIsNone(self.store().get_confirmation(self.sender))

    def test_missing_and_invalid_stock_fields_do_not_mutate(self):
        store = self.store()
        store.set_stock(Resource.WATER, 20)
        with self.classify_as("stock_update", {"resource": "water", "quantity": 5}):
            missing = self.say("Increase water stock")
        self.assertIn("How many units", missing)
        self.assertEqual(self.store().get_stock("water"), 20)
        invalid = self.say("ADJUST resource=water operation=add quantity=not-a-number")
        self.assertIn("whole number", invalid)
        self.assertEqual(self.store().get_stock("water"), 20)
        unsupported = self.say("ADJUST resource=gold operation=add quantity=5")
        self.assertIn("don't recognise", unsupported)
        self.assertEqual(self.store().get_stock("water"), 20)

    def test_natural_request_is_validated_then_confirmed(self):
        fields = {
            "site": "Hill Hamlet",
            "resource": "medical kits",
            "quantity": 60,
            "people_affected": 120,
            "severity": 3,
            "vulnerable_groups": ["children"],
        }
        with self.classify_as("request_create", fields):
            prompt = self.say(
                "Hill Hamlet needs 60 medical kits for 120 people, severity 3, children"
            )
        self.assertIn("Reply YES to confirm", prompt)
        self.assertEqual(self.store().all_requests(), [])
        response = self.say("yes")
        self.assertIn("Request accepted.", response)
        created = self.store().all_requests()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].quantity, 60)
        self.assertEqual(created[0].site_id, "S3")

        invalid_fields = dict(fields, site="Atlantis", severity=8)
        with self.classify_as("request_create", invalid_fields):
            invalid = self.say("Atlantis needs 60 medical kits for 120 people, severity 8")
        self.assertIn("don't know the site", invalid)
        self.assertEqual(len(self.store().all_requests()), 1)

    def test_prioritization_is_store_backed_and_allocation_is_confirmed_once(self):
        store = self.store()
        store.set_stock(Resource.WATER, 100)
        requests = [
            ResourceRequest("S1", Resource.WATER, 80, 100, 5),
            ResourceRequest("S3", Resource.WATER, 60, 100, 2),
        ]
        for request in requests:
            store.add_request(request)
        before = self.store().state
        with self.classify_as("prioritize"):
            ranked = self.say("Which village needs help most urgently?")
        self.assertIn(requests[0].request_id, ranked.splitlines()[1])
        self.assertEqual(self.store().state, before)

        with self.classify_as("allocate"):
            with patch("agent.run_round", wraps=run_round) as allocate:
                preview = self.say("Allocate available stock")
        self.assertIn("Preview for allocation round", preview)
        self.assertIn("Reply YES to confirm", preview)
        self.assertEqual(self.store().get_stock("water"), 100)
        self.assertEqual(len(self.store().all_allocations()), 0)
        allocate.assert_not_called()

        with patch("agent.run_round", wraps=run_round) as allocate:
            response = self.say("yes")
        allocate.assert_called_once()
        self.assertIn("requested=80 allocated=", response)
        self.assertIn("Remaining stock: water=0", response)
        self.assertEqual(self.store().get_stock("water"), 0)
        allocation_count = len(self.store().all_allocations())
        self.assertEqual(allocation_count, 2)
        self.assertIn("no pending action", self.say("yes").lower())
        self.assertEqual(len(self.store().all_allocations()), allocation_count)

    def test_common_read_only_questions_use_real_store_without_asi_or_mutation(self):
        store = self.store()
        store.set_stock(Resource.WATER, 37)
        requests = [
            ResourceRequest("S1", Resource.WATER, 80, 100, 5),
            ResourceRequest("S3", Resource.MEDICAL_KITS, 20, 50, 3),
        ]
        for request in requests:
            store.add_request(request)
        before = self.store().state.copy()

        with patch("agent.classify_message") as classifier:
            inventory = self.say("How much water is available?")
            highest = self.say("Which village needs help most urgently?")
            explanation = self.say("Why is it prioritized?")
            summary = self.say("Summarize pending relief needs.")

        classifier.assert_not_called()
        self.assertIn("37 litres", inventory)
        self.assertIn(requests[0].request_id, highest)
        self.assertIn("severity +50.00", explanation)
        self.assertIn("2 requests across 2 sites", summary)
        self.assertIn("S1 | water | requested=80", summary)
        self.assertIn("S3 | medical_kits | requested=20", summary)
        self.assertEqual(self.store().state, before)

    def test_common_read_only_questions_do_not_depend_on_confirmation_storage(self):
        store = self.store()
        store.set_stock(Resource.WATER, 23)
        request = ResourceRequest("S1", Resource.WATER, 12, 40, 4)
        store.add_request(request)

        with patch.object(
            Store, "get_confirmation", side_effect=AttributeError("missing method"),
        ), patch("agent.classify_message") as classifier:
            inventory = self.say("How much water is available?")
            priority = self.say("Which village needs help most urgently?")
            summary = self.say("Summarize pending relief needs.")

        classifier.assert_not_called()
        self.assertIn("23 litres", inventory)
        self.assertIn(request.request_id, priority)
        self.assertIn("S1 | water | requested=12", summary)

    def test_summary_only_includes_pending_requests(self):
        store = self.store()
        pending = ResourceRequest("S1", Resource.WATER, 10, 20, 2)
        store.add_request(pending)
        before = self.store().state.copy()
        response = self.say("SUMMARY")
        self.assertIn("1 request across 1 site", response)
        self.assertIn("S1 | water | requested=10", response)
        self.assertEqual(self.store().state, before)

    def test_empty_operational_state_is_reported_with_safe_next_steps(self):
        before = self.store().state.copy()
        with patch("agent.classify_message") as classifier:
            summary = self.say("Summarize pending relief needs.")
            priority = self.say("Which village needs help most urgently?")
            inventory = self.say("How much water is available?")

        classifier.assert_not_called()
        self.assertIn("No needs have been submitted yet", summary)
        self.assertIn("Submit a validated relief request first", priority)
        self.assertIn("Current water inventory: 0 litres", inventory)
        self.assertEqual(self.store().state, before)

    def test_unexpected_failure_is_logged_with_stage_and_safe_diagnostic(self):
        with patch("agent.classify_message", side_effect=RuntimeError("private detail")):
            response = self.say("Please check the water situation")
        self.assertIn("ASI1 intent classification", response)
        self.assertIn("RuntimeError", response)
        self.assertNotIn("private detail", response)
        self.assertTrue(any(
            "ASI1 intent classification (RuntimeError)" in entry
            for entry in self.ctx.logger.errors
        ))

    def test_natural_delivery_confirmation_uses_existing_transition(self):
        store = self.store()
        delivery = Delivery(
            delivery_id="DEL-TEST",
            allocation_id="ALC-TEST",
            site_id="S1",
            resource=Resource.WATER,
            sent_qty=5,
            status=DeliveryStatus.IN_TRANSIT,
        )
        store.add_delivery(delivery)
        fields = {
            "delivery_id": delivery.delivery_id,
            "received_qty": 5,
            "confirmed_by": "Test recipient",
        }
        with self.classify_as("delivery_confirm", fields):
            prompt = self.say("The recipient received five; confirmed by Test recipient")
        self.assertIn("Reply YES to confirm", prompt)
        self.assertEqual(self.store().get_delivery(delivery.delivery_id).status,
                         DeliveryStatus.IN_TRANSIT)
        result = self.say("yes")
        self.assertIn("Delivery confirmed", result)
        self.assertEqual(self.store().get_delivery(delivery.delivery_id).status,
                         DeliveryStatus.DELIVERED)

    def test_dispatch_and_rerouting_require_confirmation(self):
        store = self.store()
        allocation = Allocation(
            allocation_id="ALC-TEST",
            request_id="REQ-TEST",
            site_id="S1",
            resource=Resource.WATER,
            requested_qty=10,
            allocated_qty=10,
            delivery_id="DEL-DISPATCH",
        )
        store.add_allocation(allocation)
        store.add_delivery(Delivery(
            delivery_id=allocation.delivery_id,
            allocation_id=allocation.allocation_id,
            site_id=allocation.site_id,
            resource=allocation.resource,
            sent_qty=allocation.allocated_qty,
        ))
        dispatch_plan = DispatchPlan(
            request_id="REQ-TEST",
            site_id="S1",
            resource="water",
            quantity=10,
            depot_id="D1",
            distance_km=4.5,
            eta_minutes=8,
            route="test route",
        )
        with patch("agent.plan_dispatch", return_value=dispatch_plan) as plan_dispatch:
            prompt = self.say("DISPATCH delivery_id=DEL-DISPATCH")
            self.assertIn("Reply YES to confirm", prompt)
            self.assertEqual(
                self.store().get_delivery("DEL-DISPATCH").status,
                DeliveryStatus.ALLOCATED,
            )
            result = self.say("yes")
        plan_dispatch.assert_called_once()
        self.assertIn("Delivery dispatched", result)
        self.assertEqual(
            self.store().get_delivery("DEL-DISPATCH").status,
            DeliveryStatus.DISPATCHED,
        )

        reroute_delivery = Delivery(
            delivery_id="DEL-REROUTE",
            allocation_id="ALC-OTHER",
            site_id="S1",
            resource=Resource.WATER,
            sent_qty=5,
            depot_id="D1",
            status=DeliveryStatus.IN_TRANSIT,
        )
        self.store().add_delivery(reroute_delivery)
        route_plan = RoutePlan("D2", 6.0, 12, "alternate route", "test")
        with patch("agent.find_alternative_route", return_value=route_plan) as find_route:
            prompt = self.say("REROUTE delivery_id=DEL-REROUTE")
            self.assertIn("Reply YES to confirm", prompt)
            self.assertEqual(
                self.store().get_delivery("DEL-REROUTE").status,
                DeliveryStatus.IN_TRANSIT,
            )
            result = self.say("yes")
        find_route.assert_called_once()
        self.assertIn("Delivery rerouted", result)
        self.assertEqual(
            self.store().get_delivery("DEL-REROUTE").status,
            DeliveryStatus.REROUTED,
        )

    def test_asi_extraction_is_validated_against_user_text(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                content = json.dumps({
                    "intent": "stock_update",
                    "fields": {
                        "resource": "water",
                        "operation": "add",
                        "quantity": 2000,
                    },
                })
                return {"choices": [{"message": {"content": content}}]}

        with patch.dict(os.environ, {"ASI1_API_KEY": "test-only"}, clear=True):
            with patch("agent.requests.post", return_value=Response()):
                result = classify_message("Add two thousand units of water")
        self.assertEqual(result["fields"]["quantity"], 2000)
        self.assertEqual(result["fields"]["resource"], "water")

        fabricated = {
            "resource": "water",
            "operation": "add",
            "quantity": 2000,
            "delivery_id": "DEL-FAKE123",
        }
        class FabricatedResponse(Response):
            def json(self):
                content = json.dumps({
                    "intent": "stock_update",
                    "fields": fabricated,
                })
                return {"choices": [{"message": {"content": content}}]}

        with patch.dict(os.environ, {"ASI1_API_KEY": "test-only"}, clear=True):
            with patch("agent.requests.post", return_value=FabricatedResponse()):
                result = classify_message("Add water")
        self.assertIsNone(result["fields"]["quantity"])
        self.assertIsNone(result["fields"]["delivery_id"])

    def test_malformed_asi_output_uses_safe_unavailable_path(self):
        class MalformedResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "not-json"}}]}

        with patch("config.load_local_environment"):
            with patch.dict(os.environ, {"ASI1_API_KEY": "test-only"}, clear=True):
                with patch("agent.requests.post", return_value=MalformedResponse()):
                    with self.assertRaises(IntentServiceUnavailable):
                        classify_message("How much water is left?")
        with patch("config.load_local_environment"):
            with patch.dict(os.environ, {"ASI1_API_KEY": ""}, clear=True):
                with self.assertRaises(MissingASIAPIKey):
                    classify_message("How much water is left?")

    def test_asi_http_failures_have_safe_actionable_reason(self):
        class FailedResponse:
            status_code = 503

            def raise_for_status(self):
                raise __import__("requests").HTTPError(
                    "service unavailable", response=self,
                )

        with patch("config.load_local_environment"):
            with patch.dict(os.environ, {"ASI1_API_KEY": "test-only"}, clear=True):
                with patch("agent.requests.post", return_value=FailedResponse()):
                    with self.assertRaises(IntentServiceUnavailable) as raised:
                        classify_message("Please check the water situation")
        self.assertEqual(raised.exception.reason, "HTTP status 503")

    def test_missing_key_has_clear_safe_chat_response(self):
        with patch("agent.classify_message", side_effect=MissingASIAPIKey):
            response = self.say("Please explain our logistics workflow.")
        self.assertIn("ASI1_API_KEY is missing", response)
        self.assertIn("Agentverse secrets", response)

    def test_local_environment_loader_and_platform_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "ASI1_API_KEY=local-test-value\n"
                "RELIEFSTOCK_LABEL='local coordinator'\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                config.load_local_environment(env_path)
                self.assertEqual(os.environ[config.ASI1_API_KEY_ENV_VAR], "local-test-value")
                self.assertEqual(os.environ["RELIEFSTOCK_LABEL"], "local coordinator")
                os.environ[config.ASI1_API_KEY_ENV_VAR] = "platform-test-value"
                config.load_local_environment(env_path)
                self.assertEqual(os.environ[config.ASI1_API_KEY_ENV_VAR], "platform-test-value")

    def test_configured_key_is_used_for_asi_request(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "intent": "inventory_query",
                                "fields": {"resource": "water"},
                            })
                        }
                    }]
                }

        with patch("config.load_local_environment"):
            with patch.dict(os.environ, {"ASI1_API_KEY": "configured-test-value"}, clear=True):
                with patch("agent.requests.post", return_value=Response()) as post:
                    result = classify_message("How much water is left?")
        self.assertEqual(result["intent"], "inventory_query")
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer configured-test-value",
        )


    def test_asi_runs_if_deployed_config_has_no_local_env_loader(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "intent": "help",
                                "fields": {},
                            })
                        }
                    }]
                }

        with patch.dict(config.__dict__, {"load_local_environment": None}):
            with patch.dict(os.environ, {"ASI1_API_KEY": "test-only"}, clear=True):
                with patch("agent.requests.post", return_value=Response()) as post:
                    result = classify_message("Please explain our workflow")
        self.assertEqual(result["intent"], "help")
        post.assert_called_once()

    def test_demo_dataset_requires_confirmation_and_only_loads_into_empty_store(self):
        with patch("agent.classify_message") as classifier:
            preview = self.say("DEMO")
            self.assertIn("Reply YES to confirm", preview)
            self.assertEqual(self.store().all_requests(), [])
            loaded = self.say("YES")

        classifier.assert_not_called()
        self.assertIn("10 pending requests", loaded)
        self.assertEqual(len(self.store().open_requests()), 10)
        self.assertEqual(
            self.store().get_inventory(),
            {"water": 2000, "medical_kits": 150, "blankets": 500, "food_packs": 300},
        )
        requests_before = len(self.store().all_requests())
        preview = self.say("DEMO")
        self.assertIn("Reply YES to confirm", preview)
        blocked = self.say("YES")
        self.assertIn("already loaded and active", blocked)
        self.assertIn("No duplicate records were created", blocked)
        self.assertEqual(len(self.store().all_requests()), requests_before)

    def test_unavailable_asi_falls_back_without_breaking_structured_commands(self):
        self.store().set_stock(Resource.WATER, 42)
        with patch("agent.classify_message", side_effect=IntentServiceUnavailable):
            fallback = self.say("Please check the water situation")
        self.assertIn("structured commands", fallback)
        structured = self.say("INVENTORY resource=water")
        self.assertIn("42 litres", structured)

    def test_numeric_shorthand_is_checked_against_message(self):
        fields = sanitize_fields(
            "Increase our water stock by 2k",
            {"resource": "water", "operation": "add", "quantity": 2000},
        )
        self.assertEqual(fields["quantity"], 2000)
        self.assertEqual(fields["operation"], "add")

    def test_demo_loads_when_inventory_already_exists_and_preserves_higher_stock(self):
        self.store().set_stock(Resource.WATER, 4000)
        self.assertEqual(self.store().get_stock("water"), 4000)
        preview = self.say("DEMO")
        self.assertIn("Reply YES to confirm", preview)
        loaded = self.say("YES")
        self.assertIn("10 pending requests", loaded)
        self.assertEqual(len(self.store().open_requests()), 10)
        # Water stock preserved at 4000 (higher than 2000 demo default)
        self.assertEqual(self.store().get_stock("water"), 4000)
        # Other demo resources supplied
        self.assertEqual(self.store().get_stock("medical_kits"), 150)
        self.assertEqual(self.store().get_stock("blankets"), 500)

    def test_natural_request_creation_with_site_name_fallback_and_confirmation(self):
        with self.classify_as("request_create", {
            "site_name": "Hill Hamlet",
            "resource": "water",
            "quantity": 100,
            "people_affected": 50,
            "severity": 5,
        }):
            prompt = self.say("Hill Hamlet needs 100 water units for 50 people. Severity is 5.")
        self.assertIn("Submit request for 100 water at S3", prompt)
        self.assertIn("Reply YES to confirm", prompt)
        self.assertEqual(len(self.store().all_requests()), 0)

        response = self.say("YES")
        self.assertIn("Request accepted", response)
        self.assertIn("S3", response)
        self.assertEqual(len(self.store().open_requests()), 1)

        requests_list = self.say("REQUESTS")
        self.assertIn("S3 | water | requested=100 allocated=0 | PENDING", requests_list)

    def test_natural_delivery_tracking_retrieves_status_and_trace(self):
        from models import Delivery, DeliveryStatus, EventType
        delivery = Delivery(
            delivery_id="DEL-TEST001",
            allocation_id="ALC-001",
            site_id="S3",
            resource=Resource.WATER,
            sent_qty=100,
            depot_id="D1",
            status=DeliveryStatus.DISPATCHED,
            distance_km=14.5,
            eta_minutes=25,
            route="D1 -> S3 via Main Highway",
        )
        self.store().add_delivery(delivery)
        self.store().log(EventType.DISPATCHED, "Dispatched 100 water to S3", delivery_id="DEL-TEST001")

        result = self.say("Where is delivery DEL-TEST001?")
        self.assertIn("Delivery DEL-TEST001: Status DISPATCHED", result)
        self.assertIn("Hill Hamlet (S3)", result)
        self.assertIn("14.50 km", result)
        self.assertIn("Delivery trace:", result)

    def test_incomplete_delivery_id_prompts_for_complete_id(self):
        result = self.say("Where is delivery DEL-")
        self.assertIn("Which delivery ID should I trace?", result)
        self.assertIn("Please specify a complete ID", result)

    def test_natural_request_parsed_locally_without_asi(self):
        with patch("agent.classify_message") as classifier:
            prompt = self.say("Hill Hamlet needs 60 medical kits for 60 people, severity 3")
            classifier.assert_not_called()
        self.assertIn("Submit request for 60 medical_kits at S3", prompt)
        self.assertIn("Reply YES to confirm", prompt)

        response = self.say("YES")
        self.assertIn("Request accepted", response)
        self.assertIn("S3", response)
        self.assertEqual(len(self.store().open_requests()), 1)

    def test_unknown_site_request_prompts_for_coordinates(self):
        with patch("agent.classify_message") as classifier:
            response = self.say("A village needs 100 litres of water for 50 people. Severity is 5.")
            classifier.assert_not_called()
        self.assertIn("Request rejected:", response)
        self.assertIn("I don't know the site 'A village'", response)
        self.assertIn("latitude and longitude", response)
        self.assertEqual(len(self.store().all_requests()), 0)

    def test_allocation_outcome_reports_unmet_and_non_delivery_notice(self):
        self.store().set_stock(Resource.WATER, 100)
        req1 = ResourceRequest(site_id="S1", resource=Resource.WATER, quantity=80, people_affected=100, severity=5)
        req2 = ResourceRequest(site_id="S3", resource=Resource.WATER, quantity=60, people_affected=60, severity=3)
        self.store().add_request(req1)
        self.store().add_request(req2)

        preview = self.say("Allocate available stock fairly")
        self.assertIn("requested=80 allocated=80 unmet=0", preview)
        self.assertIn("requested=60 allocated=20 unmet=40", preview)
        self.assertIn("Reply YES to confirm", preview)

        completed = self.say("YES")
        self.assertIn("Allocation round 1 completed.", completed)
        self.assertIn("requested=80 allocated=80 unmet=0", completed)
        self.assertIn("requested=60 allocated=20 unmet=40", completed)
        self.assertIn("Resource Breakdown:", completed)
        self.assertIn("- water: requested 140, allocated 100, unmet 40, remaining stock 0", completed)
        self.assertIn("Note: Deliveries have been registered in the ledger. Supplies have not yet been physically dispatched or delivered.", completed)

    def test_how_to_request_and_donate_guidance(self):
        req_guide = self.say("How can I request blankets, tents, and hygiene kits for a relief camp in my area?")
        self.assertIn("To submit a relief request", req_guide)
        self.assertIn("Site name", req_guide)
        self.assertIn("Severity level", req_guide)

        donate_guide = self.say("How do I donate supplies or register incoming inventory with the relief stock system?")
        self.assertIn("To register incoming inventory", donate_guide)
        self.assertIn("Add 2000 litres of water", donate_guide)


if __name__ == "__main__":
    import unittest

    unittest.main()

