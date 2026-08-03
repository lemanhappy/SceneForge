import tempfile
import unittest

from server.lora_api import LoraAPI
from server.lora_service import LoraService
from services.lora_runtime import LoraAwareImageGenerator, with_project_loras
from services.workflow_engine import WorkflowEngine
from agent_runtime.session_index import SessionIndex


def _payload(**overrides):
    value = {
        "lora_id": "hero_lora",
        "display_name": "主角 LoRA",
        "provider": "comfyui",
        "base_model": "FLUX.1-dev",
        "source_type": "local",
        "model_ref": "D:/models/hero.safetensors",
        "trigger_words": "hero_face, black glasses",
        "default_weight": 0.85,
        "application_mode": "native",
        "tags": "角色, 写实",
        "enabled": True,
    }
    value.update(overrides)
    return value


class LoraServiceTests(unittest.TestCase):
    def test_crud_and_resolve(self):
        with tempfile.TemporaryDirectory() as root:
            service = LoraService(root)
            item = service.upsert(_payload())
            self.assertEqual(item["trigger_words"], ["hero_face", "black glasses"])
            self.assertEqual(service.resolve(["hero_lora"])[0]["default_weight"], 0.85)
            with self.assertRaises(FileExistsError):
                service.upsert(_payload())
            updated = service.upsert(_payload(default_weight=1.0), lora_id="hero_lora", overwrite=True)
            self.assertEqual(updated["default_weight"], 1.0)
            self.assertTrue(service.delete("hero_lora"))
            self.assertFalse(service.delete("hero_lora"))

    def test_validation_and_disabled_selection(self):
        with tempfile.TemporaryDirectory() as root:
            service = LoraService(root)
            with self.assertRaises(ValueError):
                service.upsert(_payload(lora_id="../bad"))
            with self.assertRaises(ValueError):
                service.upsert(_payload(model_ref=""))
            service.upsert(_payload(enabled=False))
            with self.assertRaisesRegex(ValueError, "已停用"):
                service.resolve(["hero_lora"])


class _Generator:
    def __init__(self, supports_lora=False):
        self.supports_lora = supports_lora
        self.calls = []

    async def generate_single_image(self, prompt, reference_image_paths=None, **kwargs):
        self.calls.append((prompt, kwargs))
        return "image"


class LoraRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_trigger_mode_reaches_prompt_and_structured_kwargs(self):
        inner = _Generator()
        wrapper = with_project_loras(inner, {"lora_bindings": [_payload(application_mode="trigger")]})
        self.assertIsInstance(wrapper, LoraAwareImageGenerator)
        result = await wrapper.generate_single_image("电影感人物特写", [])
        self.assertEqual(result, "image")
        self.assertIn("hero_face", inner.calls[0][0])
        self.assertEqual(inner.calls[0][1]["lora_bindings"][0]["model_ref"], "D:/models/hero.safetensors")

    async def test_native_mode_rejects_unsupported_provider(self):
        wrapper = LoraAwareImageGenerator(_Generator(), [_payload()])
        with self.assertRaisesRegex(RuntimeError, "不支持原生 LoRA"):
            await wrapper.generate_single_image("portrait", [])

    async def test_native_mode_passes_to_supported_provider(self):
        inner = _Generator(supports_lora=True)
        wrapper = LoraAwareImageGenerator(inner, [_payload()])
        await wrapper.generate_single_image("portrait", [])
        self.assertEqual(inner.calls[0][1]["lora_bindings"][0]["weight"], 0.85)


class _Workflow(WorkflowEngine):
    async def _gen_script(self, session, instruction=""):
        return "剧本已生成"


class LoraProjectIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_lora_is_snapshotted_into_project(self):
        with tempfile.TemporaryDirectory() as root:
            catalog = LoraService(root)
            catalog.upsert(_payload(application_mode="trigger"))
            index = SessionIndex(root)
            engine = _Workflow(index, root, lora_service=catalog)

            result = await engine.start_topic("程序员逆袭", lora_ids=["hero_lora"])

            record = index.get(result["session_id"])
            self.assertEqual(record["lora_ids"], ["hero_lora"])
            self.assertEqual(record["lora_bindings"][0]["trigger_words"], ["hero_face", "black glasses"])
            catalog.upsert(_payload(application_mode="trigger", trigger_words="changed"), lora_id="hero_lora", overwrite=True)
            self.assertEqual(index.get(result["session_id"])["lora_bindings"][0]["trigger_words"], ["hero_face", "black glasses"])


class LoraApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes(self):
        with tempfile.TemporaryDirectory() as root:
            api = LoraAPI(LoraService(root))
            status, result = await api.handle("POST", "/api/loras", _payload())
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"])
            self.assertEqual((await api.handle("POST", "/api/loras", _payload()))[0], 409)
            self.assertEqual(len((await api.handle("GET", "/api/loras"))[1]["loras"]), 1)
            self.assertEqual((await api.handle("DELETE", "/api/loras/hero_lora"))[0], 200)


if __name__ == "__main__":
    unittest.main()
