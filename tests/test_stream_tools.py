"""流式工具调用的组装、执行与回放：一轮里**多个**调用（模型合并调用来省往返）。

模型会在一轮响应里一次给出多个 function_call / tool_calls，而且分片是**交错**到达的
（同一轮里调用 A 的参数碎片和调用 B 的混在一起）。这里盯住四件事：

1. 分片按 index / item_id 各归各家，不按到达顺序拼；
2. 一个调用**输出完整不等于可以执行**——执行发生在整条流结束之后，本轮调用按原顺序逐个执行；
3. 回放形状：chat_completions 是「一个 assistant(全部 tool_calls) + 每条 tool 结果」，
   responses 是每个调用一份 function_call + function_call_output；
4. 被输出上限截断、或上游发来 response.incomplete 时，报错要说出原因，而不是笼统的
   「模型未输出任何内容」。

全部离线：假客户端只吐事件对象，`run_tool` 默认只记录（不执行）。只有最后一条用例真的走一遍
tools.execute_tool，读本仓库自己的文件。
"""
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import tests.support  # noqa: F401  （把仓库根加进 sys.path）

from app import core, llm, tools
from tests.support import TEST_CARD, TEST_PRESET

# 一轮两个调用 = 两次请求；这里只要 read_file（直接工具路径，用例不碰 world）
BASE_CONFIG = dict(tests.support.TEST_CONFIG["endpoints"][0])
BASE_CONFIG.update(tools=["read_file"], ptc=False, reasoning_effort="low")

USER_ITEM = [{"role": "user", "content": "看看"}]
USER_INPUT_ITEM = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "看看"}]}]


class Obj:
    """最小事件对象：属性取不到返回 None，同时支持下标与 .get()（对应 SDK 对象的两种读法）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name):
        return None

    def get(self, name, default=None):
        return self.__dict__.get(name, default)

    def __getitem__(self, name):
        return self.__dict__[name]


def _chunk(delta=None, finish_reason=None):
    """一条 chat_completions 流式分片。"""
    return Obj(choices=[Obj(delta=Obj(**(delta or {})), finish_reason=finish_reason)])


def _call(index, id=None, name=None, arguments=None):
    """delta.tool_calls 里的一项。"""
    return Obj(index=index, id=id, type="function", function=Obj(name=name, arguments=arguments))


def _ev(etype, **kw):
    """一条 responses 流式事件。"""
    return Obj(type=etype, **kw)


class _Stream:
    """把一批事件包成异步可迭代对象（SDK 的流也是这么用的：async for）。"""

    def __init__(self, events):
        self.events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)


class _Replay:
    """按次数回放预先写好的一批批事件，并记下每次请求带了什么。"""

    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.requests = []

    def __call__(self, **kwargs):
        # messages 是 chat_completions 的输入字段，input 是 responses 的
        self.requests.append(kwargs.get("messages", kwargs.get("input")))
        return _Stream(self.rounds.pop(0))

    @property
    def last_request(self):
        return self.requests[-1]


class _Collect:
    """收集 stream_body 的事件，并记录收到的工具调用（默认不真执行）。"""

    def __init__(self):
        self.events = []
        self.calls = []

    async def run_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return f"结果 {len(self.calls)}"

    async def drive(self, config, replay, input_items=None):
        client = mock.MagicMock()
        client.chat.completions.create = mock.AsyncMock(side_effect=replay)
        client.responses.create = mock.AsyncMock(side_effect=replay)
        cm = mock.MagicMock()
        cm.__aenter__ = mock.AsyncMock(return_value=client)
        cm.__aexit__ = mock.AsyncMock(return_value=False)
        with mock.patch.object(llm, "_make_client", lambda cfg: cm):
            async for event in llm.stream_body(
                input_items if input_items is not None else USER_ITEM, config, self.run_tool
            ):
                self.events.append(event)

    @property
    def done(self):
        return next((e for e in self.events if e["type"] == "done"), None)

    @property
    def tool_events(self):
        return [e for e in self.events if e["type"] == "tool"]


class StreamToolCallsTest(unittest.IsolatedAsyncioTestCase):

    # ---------------- chat_completions ----------------

    async def test_chat_completions_batches_two_calls(self):
        """一轮两个调用：分片交错也要各归各家，执行完再带着两条结果发下一轮。"""
        replay = _Replay(
            [
                _chunk({"content": "我去翻两页。", "reasoning_content": "先查 A 再查 B。"}),
                _chunk({"tool_calls": [_call(0, id="call_a", name="read_file", arguments='{"file_')]}),
                _chunk({"tool_calls": [_call(1, id="call_b", name="read_file", arguments='{"file_path": "b')]}),
                _chunk({"tool_calls": [_call(0, arguments='path": "a.py"}')]}),
                _chunk({"tool_calls": [_call(1, arguments='.py"}')]}),
                _chunk({}, finish_reason="tool_calls"),
            ],
            [_chunk({"content": "看完了。", "reasoning_content": "可以回话了。"}, finish_reason="stop")],
        )
        col = _Collect()
        await col.drive(dict(BASE_CONFIG, api_type="chat_completions"), replay)

        self.assertEqual(
            col.calls, [("read_file", '{"file_path": "a.py"}'), ("read_file", '{"file_path": "b.py"}')]
        )
        # 工具事件逐个到达，每个都带自己的完整参数与结果
        self.assertEqual([e["result"] for e in col.tool_events], ["结果 1", "结果 2"])
        # 第二轮请求：一个 assistant 带全部 tool_calls，其后每条结果一条 tool 消息，顺序一致
        messages = replay.last_request
        assistant = [m for m in messages if m.get("tool_calls")]
        self.assertEqual(len(assistant), 1)
        self.assertEqual([c["id"] for c in assistant[0]["tool_calls"]], ["call_a", "call_b"])
        self.assertEqual(
            [(m["role"], m.get("tool_call_id")) for m in messages if m["role"] == "tool"],
            [("tool", "call_a"), ("tool", "call_b")],
        )
        self.assertEqual(col.done["content"], "我去翻两页。看完了。")

    async def test_chat_completions_truncated_round_names_reason(self):
        """被输出上限压空：报错要带上 finish_reason，别让人去猜。"""
        col = _Collect()
        with self.assertRaises(RuntimeError) as ctx:
            await col.drive(dict(BASE_CONFIG, api_type="chat_completions"),
                            _Replay([_chunk({}, finish_reason="length")]))
        self.assertIn("finish_reason=length", str(ctx.exception))

    async def test_chat_completions_empty_round_without_reason(self):
        """上游没给结束原因时，退回原来的说法，不硬编一句可能错的解释。"""
        col = _Collect()
        with self.assertRaises(RuntimeError) as ctx:
            await col.drive(dict(BASE_CONFIG, api_type="chat_completions"), _Replay([_chunk({})]))
        self.assertEqual(str(ctx.exception), "模型未输出任何内容")

    # ---------------- responses ----------------

    @staticmethod
    def _responses_calls_round():
        return [
            _ev("response.output_item.added", item=Obj(id="rs_1", type="reasoning")),
            _ev("response.reasoning_text.delta", item_id="rs_1", delta="要查两处。"),
            _ev("response.output_item.added",
                item=Obj(id="fc_1", type="function_call", call_id="c1", name="read_file")),
            _ev("response.output_item.added",
                item=Obj(id="fc_2", type="function_call", call_id="c2", name="read_file")),
            _ev("response.function_call_arguments.delta", item_id="fc_1", delta='{"file_path": "a'),
            _ev("response.function_call_arguments.delta", item_id="fc_2", delta='{"file_path": "b'),
            _ev("response.function_call_arguments.delta", item_id="fc_1", delta='.py"}'),
            _ev("response.function_call_arguments.delta", item_id="fc_2", delta='.py"}'),
            _ev("response.output_item.done", item=Obj(id="fc_1", arguments='{"file_path": "a.py"}')),
            _ev("response.output_item.done", item=Obj(id="fc_2", arguments='{"file_path": "b.py"}')),
        ]

    async def test_responses_batches_two_calls(self):
        """一轮两个 function_call：按 item_id 归位，回放是成对的 function_call + 输出。"""
        replay = _Replay(
            self._responses_calls_round(),
            [
                _ev("response.output_item.added", item=Obj(id="msg_1", type="message")),
                _ev("response.output_text.delta", item_id="msg_1", delta="看完了。"),
            ],
        )
        col = _Collect()
        await col.drive(dict(BASE_CONFIG, api_type="responses"), replay, input_items=USER_INPUT_ITEM)

        self.assertEqual(
            col.calls, [("read_file", '{"file_path": "a.py"}'), ("read_file", '{"file_path": "b.py"}')]
        )
        items = replay.last_request
        calls = [(i["call_id"], i["arguments"]) for i in items if i["type"] == "function_call"]
        outputs = [i["call_id"] for i in items if i["type"] == "function_call_output"]
        self.assertEqual(calls, [("c1", '{"file_path": "a.py"}'), ("c2", '{"file_path": "b.py"}')])
        self.assertEqual(outputs, ["c1", "c2"])
        # responses 的硬约束：每个 function_call 前各要一份 reasoning_text（第二份是单空格占位）
        kinds = [i["type"] for i in items]
        self.assertEqual(kinds.count("reasoning"), 2)
        first_call = kinds.index("function_call")
        second_call = kinds.index("function_call", first_call + 1)
        self.assertEqual(items[first_call - 1]["type"], "reasoning")
        self.assertEqual(items[second_call - 1]["type"], "reasoning")
        self.assertEqual(items[second_call - 1]["content"][0]["text"], " ")
        self.assertEqual(col.done["content"], "看完了。")

    async def test_responses_incomplete_round_names_reason(self):
        """response.incomplete：流是正常结束的，只有这个事件说得清为什么没内容。"""
        incomplete = Obj(status="incomplete", incomplete_details=Obj(reason="max_output_tokens"))
        col = _Collect()
        with self.assertRaises(RuntimeError) as ctx:
            await col.drive(dict(BASE_CONFIG, api_type="responses"),
                            _Replay([_ev("response.incomplete", response=incomplete)]),
                            input_items=USER_INPUT_ITEM)
        self.assertIn("max_output_tokens", str(ctx.exception))

    # ---------------- 真执行一遍 ----------------

    async def test_batch_really_executes_both_calls(self):
        """两条 read_file 交给真的 tools.execute_tool：一批里每条都执行，谁也不吞谁。"""
        tmp = tempfile.TemporaryDirectory(prefix="rchat-streamtools-")
        self.addCleanup(tmp.cleanup)
        original = core.SESSIONS_DIR
        self.addCleanup(setattr, core, "SESSIONS_DIR", original)
        core.SESSIONS_DIR = pathlib.Path(tmp.name)

        key = "probe"
        core._session_dir(key).mkdir(parents=True)
        core.save_state({
            "id": key, "name": key, "owner": None, "preset": TEST_PRESET, "card": TEST_CARD,
            "card_name": TEST_CARD, "beginning_index": 0, "beginning_text": "", "created_at": 1.0,
            "chat_id": "c", "endpoint": 0, "temperature": 0.7, "max_tokens": 1000,
            "reasoning_effort": "low", "options_enabled": False,
        })
        core.save_history(key, [])

        results = [
            # 相对路径以角色卡目录（games/）为基准，所以仓库里的文件要往上走一级
            await tools.execute_tool(key, "read_file", json.dumps({"file_path": p, "limit": 1}))
            for p in ("../app/llm.py", "../app/tools.py")
        ]
        for result, name in zip(results, ("llm.py", "tools.py")):
            self.assertNotIn("错误", result, result)
            self.assertIn(name, result)
            self.assertIn("<content>", result)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
