"""端点配置的归一化：`api_type` 的默认值只有一处（`config.DEFAULT_API_TYPE`）。

盯住五件事：

1. 默认值就是 `chat_completions`，且只定义在 `app/config.py`；
2. 端点不写 `api_type` 时由解析器补全 —— 取的是那一个常量，不是别处硬编码的另一份；
3. 显式写的值原样保留（`responses` 这条老路径仍然可用）；
4. 写错的值（`"chat"` / `"respons"`）在解析配置时就报错，不允许「不等于 chat_completions
   就走 responses」这种静默换一套请求形状的兜底；
5. 下游拿到的 `TurnConfig` 里 `endpoint.api_type` 一定存在 —— 解析器补全 + build_turn 一条链。
"""
import unittest
from unittest import mock

import tests.support  # noqa: F401  （把仓库根加进 sys.path）

from app import config, core


def _raw(**endpoint) -> dict:
    ep = {"api_base": "https://api.example.com", "model": "m", "api_key": "k"}
    ep.update(endpoint)
    return {"endpoints": [ep]}


class EndpointApiTypeTest(unittest.TestCase):
    def test_default_is_chat_completions(self):
        self.assertEqual(config.DEFAULT_API_TYPE, "chat_completions")

    def test_omitted_value_filled_from_single_constant(self):
        """不写 api_type：补成 DEFAULT_API_TYPE，而不是别处硬编码的另一份。"""
        settings = config.Settings.from_mapping(_raw())
        self.assertEqual(settings.endpoints[0].api_type, config.DEFAULT_API_TYPE)

    def test_explicit_value_kept(self):
        for value in ("responses", "chat_completions"):
            with self.subTest(api_type=value):
                settings = config.Settings.from_mapping(_raw(api_type=value))
                self.assertEqual(settings.endpoints[0].api_type, value)

    def test_invalid_value_rejected(self):
        """像 "chat" / "respons" 这种拼错的值必须在解析配置时就炸出来。"""
        with self.assertRaises(ValueError) as ctx:
            config.Settings.from_mapping(_raw(api_type="chat"))
        self.assertIn("api_type", str(ctx.exception))

    def test_turn_carries_endpoint_api_type(self):
        """链尾：build_turn 产出的 TurnConfig 里，api_type 直接可读（不再靠 get 兜底）。"""
        settings = config.Settings.from_mapping(_raw(api_type="responses"))
        preset = {"id": "p", "tools": []}
        state = {"preset": "p", "card": "c", "beginning_text": ""}
        with (
            mock.patch.object(core, "load_settings", return_value=settings),
            mock.patch.object(core, "load_presets", return_value={"p": preset}),
        ):
            turn = core.build_turn(state)
        self.assertEqual(turn.endpoint.api_type, "responses")
        self.assertEqual(turn.endpoint.model, "m")


if __name__ == "__main__":
    unittest.main()
