# -*- coding: utf-8 -*-
"""`export` 的语音接线：`--transcribe` 就地补缓存，文字进同一时间线。

为什么值得单独测：这条路的**契约是顺序**——转写必须发生在任何一条消息被渲染
之前（`render_voice` 只读缓存），而失败模式是静默的：顺序反了 or 忘了传 key，
表现都是"导出正常、语音处是占位符"，看不出是没转写还是转写失败。
"""
import importlib.util
import sqlite3
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location('export_chat_html', SCRIPTS / 'export_chat_html.py')
export = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'sqlcipher3': types.SimpleNamespace(dbapi2=sqlite3)}):
    spec.loader.exec_module(export)

# export 实际使用的 wechat_voice 模块对象。**不能事后再 import**：
# `patch.dict(sys.modules, ...)` 退出时会移除块内加载的模块，之后 import
# 会得到另一份对象，patch 它不影响 export 看到的那份（实测踩过）。
wechat_voice = export.wechat_voice  # noqa: E402


def args_ns(**overrides):
    base = {'db': 'db', 'key': 'k', 'salt': 's', 'passphrase': 'p',
            'talker': 't', 'transcribe': False}
    base.update(overrides)
    return types.SimpleNamespace(**base)


class FakeCache:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    def get(self, key):
        return self.mapping.get(key)

    def put(self, key, text):
        self.mapping[key] = text


class InitVoiceStateTests(unittest.TestCase):

    def setUp(self):
        export.VOICE_STATE.clear()

    def test_默认不转写_不出网不花钱(self):
        """没有 `--transcribe` 时**一次都不调** `ensure_transcripts`。
        导出的主用途是翻阅/归档/分享，默认出网+花钱是越界。"""
        blob = b'\x02SILK' + b'a' * 40
        with patch.object(wechat_voice, 'load_voice_map', return_value={1: blob}), \
             patch.object(wechat_voice, 'TranscriptCache', return_value=FakeCache()), \
             patch.object(wechat_voice, 'ensure_transcripts') as ensure:
            logs = []
            export.init_voice_state(args_ns(), log=logs.append)
        ensure.assert_not_called()
        self.assertTrue(any('1 clip(s), 0 transcribed' in line for line in logs),
                        '报告要说出"有多少条、转了几条"——0 转写必须可见')

    def test_transcribe时补缓存_密钥从配置走(self):
        """`--transcribe` → `ensure_transcripts(engine='auto', 两把 key)`。
        key 从配置解密后传入（调用方负责），不经过 argv。"""
        blob = b'\x02SILK' + b'b' * 40
        with patch.object(wechat_voice, 'load_voice_map', return_value={1: blob}), \
             patch.object(wechat_voice, 'TranscriptCache', return_value=FakeCache()), \
             patch.object(wechat_voice, 'ensure_transcripts', return_value=(1, 0, 0)) as ensure, \
             patch('_utils.get_minimax_key', return_value='mm-key'), \
             patch('_utils.get_dashscope_key', return_value='ds-key'), \
             patch('_utils.load_config', return_value={}):
            logs = []
            export.init_voice_state(args_ns(transcribe=True), log=logs.append)
        ensure.assert_called_once()
        kwargs = ensure.call_args.kwargs
        self.assertEqual(kwargs['engine'], 'auto')
        self.assertEqual(kwargs['minimax_key'], 'mm-key')
        self.assertEqual(kwargs['dashscope_key'], 'ds-key')
        self.assertTrue(any('Transcribed: 1 new' in line for line in logs))

    def test_没有语音时什么都不做(self):
        """0 条语音的会话：不转写、不报错（`ensure_transcripts` 一次都不调）。"""
        with patch.object(wechat_voice, 'load_voice_map', return_value={}), \
             patch.object(wechat_voice, 'TranscriptCache', return_value=FakeCache()), \
             patch.object(wechat_voice, 'ensure_transcripts') as ensure:
            export.init_voice_state(args_ns(transcribe=True), log=lambda line: None)
        ensure.assert_not_called()


class RenderTimelineTests(unittest.TestCase):
    """同一时间线的两头：有缓存 → 文字 inline；没有 → 占位符。

    `render_voice` 读模块级 `VOICE_STATE`（导出循环一边走一边渲染），所以这里
    直接摆好状态再调它——顺序由 `init_voice_state` 在渲染前被调用保证。
    """

    def _render(self, cached):
        blob = b'\x02SILK' + b'c' * 40
        export.VOICE_STATE.clear()
        export.VOICE_STATE['map'] = {1: blob}
        export.VOICE_STATE['cache'] = FakeCache(
            {wechat_voice.cache_key(blob, 'dashscope'): '认出来的字'} if cached else {})
        return export.render_voice(1, '<msg voicelength="3000"/>')

    def test_有缓存_文字inline在同一个渲染单元里(self):
        html = self._render(cached=True)
        self.assertIn('认出来的字', html)
        self.assertIn('msg-voice-text', html)
        self.assertIn('机器转写', html)

    def test_没缓存_占位符且没有文字(self):
        html = self._render(cached=False)
        self.assertIn('[语音 3″]', html)
        self.assertNotIn('msg-voice-text', html)


if __name__ == '__main__':
    unittest.main()
