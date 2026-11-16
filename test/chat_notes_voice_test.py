# -*- coding: utf-8 -*-
"""`chat_notes` 的语音接线：只读转写缓存，把 `[语音]` 换成文本。

为什么值得单独测：这条线的**失败方式是静默的** —— 缓存里没有那条转写时，行为与"接线根本没接上"
**逐字相同**（都是 `[语音]`）。实测真库时我拿到的正是 0 命中，那一刻分不清是接线坏了还是数据就是
没有；所以这里用手写夹具把两件事分开：给了转写就必须用上，没给就必须原样不动。
"""
import importlib.util
import json
import os
import sys
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts')
sys.path.insert(0, SCRIPTS)


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cn = load('chat_notes')
wv = load('wechat_voice')


class FakeCache:
    """`TranscriptCache` 的形状：内容寻址的 get/put。"""

    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, key):
        return self.mapping.get(key)

    def put(self, key, text):
        self.mapping[key] = text


def voice(local_id, blob):
    """一条语音消息（字段名照 `nt_decrypt` 的真实输出：`localType` / `localId`）。"""
    return {'localType': 34, 'localId': local_id, 'parsedContent': '[语音]'}


class VoiceWiringTests(unittest.TestCase):

    def test_有转写就换掉占位符_并标明机器转写(self):
        blob = b'\x02SILK' + b'x' * 40
        cache = FakeCache({wv.voice_key(blob): '河倉河倉，隔壁河倉啊'})
        messages = [voice(1540, blob), {'localType': 1, 'parsedContent': '在的'}]
        enriched, pending = cn.attach_voice_transcripts(messages, {1540: blob}, cache)
        self.assertEqual((enriched, pending), (1, 0))
        self.assertIn('河倉河倉', messages[0]['parsedContent'])
        # **标签必须写明未校对**：那是别人说的话被机器认出来的字，粤语/方言会有错。
        # 不标的话模型会把它当事实原文（与概念页 `verified: false` 是同一条纪律）。
        self.assertIn(cn.VOICE_LABEL, messages[0]['parsedContent'])
        self.assertEqual(messages[1]['parsedContent'], '在的', '非语音消息一个字都不该动')

    def test_没转写就原样不动_并计入缺的条数(self):
        blob = b'\x02SILK' + b'y' * 40
        messages = [voice(1, blob)]
        enriched, pending = cn.attach_voice_transcripts(messages, {1: blob}, FakeCache({}))
        self.assertEqual((enriched, pending), (0, 1))
        self.assertEqual(messages[0]['parsedContent'], '[语音]', '缺的保持原样，不许编')

    def test_空转写算缺的_不算用上(self):
        """识别器对纯静音/杂音会写下空串（`cache.put(key, '')`）——
        那是"识别了但没有内容"，不能当成"用上了"，否则报告里那个数字会虚高。"""
        blob = b'\x02SILK' + b'z' * 40
        messages = [voice(2, blob)]
        enriched, pending = cn.attach_voice_transcripts(messages, {2: blob}, FakeCache({wv.voice_key(blob): ''}))
        self.assertEqual((enriched, pending), (0, 1))
        self.assertEqual(messages[0]['parsedContent'], '[语音]')

    def test_映射里没有这条时不算崩_算缺(self):
        blob = b'\x02SILK' + b'w' * 40
        messages = [voice(3, blob)]
        enriched, pending = cn.attach_voice_transcripts(messages, {}, FakeCache({}))
        self.assertEqual((enriched, pending), (0, 1))

    def test_缓存目录只有一份定义(self):
        """三处读同一份缓存：`wechat_voice`（拥有它）、`chat_notes`、`export_chat_html`。

        2026-10-01 **真踩过**：`chat_notes` 拿自己的输出目录（`output/chat-notes`）去拼缓存路径，
        于是它读的是**自己刚建出来的那个空目录**，而真缓存在 `output/.voice-cache`。当时报告里的
        "命中 16 条"是自洽的、看不出问题 —— 只有对比缓存条数才露馅（16 vs 1788）。
        位置只要有两份定义就会漂，所以这里钉住：都引用 `wechat_voice.DEFAULT_CACHE_DIR`。
        """
        self.assertEqual(os.path.normpath(cn.VOICE_CACHE_DIR),
                         os.path.normpath(wv.DEFAULT_CACHE_DIR))
        with open(os.path.join(SCRIPTS, 'export_chat_html.py'), encoding='utf-8') as handle:
            self.assertIn('wechat_voice.DEFAULT_CACHE_DIR', handle.read(),
                          '导出侧也该引用同一份定义，而不是自己拼一个路径')

    def test_标签是常量_写死在模块上(self):
        # 契约：标签由这里定义，别处（报告、文档）引用它，避免两处各写一份
        self.assertTrue(cn.VOICE_LABEL.startswith('[语音'))
        self.assertIn('未校对', cn.VOICE_LABEL)
        self.assertEqual(cn.VOICE_TYPE, 34)


class ProviderNamespaceTests(unittest.TestCase):
    """云端/本地两条转写路的缓存命名空间与优先级。

    为什么值得钉：两条路的文字质量不同（云端商用 ASR vs 本机 small 模型），
    混在同一个 key 下会让"谁转的"不可知——而本机那条的失败模式是**说得通顺但
    内容是编的**，被当成事实引用时比空结果更贵。
    """

    def test_云端key带前缀_本地key保持原样(self):
        blob = b'\x02SILK' + b'p' * 40
        self.assertTrue(wv.cache_key(blob, 'minimax').startswith('minimax-'))
        self.assertTrue(wv.cache_key(blob, 'dashscope').startswith('dashscope-'))
        self.assertEqual(wv.cache_key(blob, 'local'), wv.voice_key(blob),
                         '本地 key 必须保持原样——老缓存不能因为加了云端就失效')

    def test_三条路都转过时优先读MiniMax(self):
        blob = b'\x02SILK' + b'q' * 40
        cache = FakeCache({wv.cache_key(blob, 'minimax'): 'MiniMax 认的字',
                           wv.cache_key(blob, 'dashscope'): '阿里认的字',
                           wv.cache_key(blob, 'local'): '本机认的字'})
        text, engine = wv.lookup_transcript(cache, blob)
        self.assertEqual((text, engine), ('MiniMax 认的字', 'minimax'))

    def test_只有DashScope时读它(self):
        blob = b'\x02SILK' + b'q2' * 40
        cache = FakeCache({wv.cache_key(blob, 'dashscope'): '阿里认的字'})
        text, engine = wv.lookup_transcript(cache, blob)
        self.assertEqual((text, engine), ('阿里认的字', 'dashscope'))

    def test_只有本地时回退本地(self):
        blob = b'\x02SILK' + b'r' * 40
        cache = FakeCache({wv.cache_key(blob, 'local'): '本机认的字'})
        text, engine = wv.lookup_transcript(cache, blob)
        self.assertEqual((text, engine), ('本机认的字', 'local'))

    def test_都没有时返回空(self):
        text, engine = wv.lookup_transcript(FakeCache({}), b'\x02SILK' + b's' * 40)
        self.assertEqual((text, engine), (None, ''))

    def test_auto_优先MiniMax其次DashScope最后本地(self):
        self.assertEqual(wv.resolve_engine('auto', 'mm-key', 'ds-key'),
                         ('minimax', '检测到 minimaxApiKey'))
        self.assertEqual(wv.resolve_engine('auto', '', 'ds-key'),
                         ('dashscope', '检测到 dashscopeApiKey'))
        provider, _ = wv.resolve_engine('auto', '', '')
        self.assertEqual(provider, 'local')

    def test_显式指定压过自动判断(self):
        self.assertEqual(wv.resolve_engine('local', 'mm-key')[0], 'local',
                         '显式 local 不许因为有 key 就被送去出网')
        self.assertEqual(wv.resolve_engine('dashscope', 'mm-key')[0], 'dashscope',
                         '显式 dashscope 不许被 MiniMax key 抢走')

    def test_旧值cloud是dashscope的别名(self):
        # `--engine cloud` 是 DashScope 引入时的旧写法，别让老命令报错
        self.assertEqual(wv.normalize_engine('cloud'), 'dashscope')
        self.assertEqual(wv.normalize_engine('minimax'), 'minimax')


class CloudLabelTests(unittest.TestCase):
    """云端转写挂云端的标签，本机的挂"未校对"——标签错了比没有标签更误导。"""

    def test_只有云端条目时用云端标签(self):
        blob = b'\x02SILK' + b'c' * 40
        cache = FakeCache({wv.cache_key(blob, 'minimax'): '商用识别结果'})
        messages = [voice(1541, blob)]
        enriched, pending = cn.attach_voice_transcripts(messages, {1541: blob}, cache)
        self.assertEqual((enriched, pending), (1, 0))
        self.assertIn('商用识别结果', messages[0]['parsedContent'])
        self.assertIn(cn.VOICE_LABEL_CLOUD, messages[0]['parsedContent'])
        self.assertNotIn(cn.VOICE_LABEL, messages[0]['parsedContent'])

    def test_两条路都有时用MiniMax那份与云端标签(self):
        blob = b'\x02SILK' + b'd' * 40
        cache = FakeCache({wv.cache_key(blob, 'minimax'): 'MiniMax 字',
                           wv.cache_key(blob, 'local'): '本机字'})
        messages = [voice(1542, blob)]
        cn.attach_voice_transcripts(messages, {1542: blob}, cache)
        self.assertIn('MiniMax 字', messages[0]['parsedContent'])
        self.assertNotIn('本机字', messages[0]['parsedContent'])


class FakeWebSocket:
    """按脚本应答的假 WebSocket：验证协议解析，不联网。

    `script` 是服务端事件序列（dict 或 bytes）；`sent` 记录客户端发出去的
    文本帧，供断言 run-task / finish-task 的报文形状。
    """

    def __init__(self, script, sent):
        self.script = list(script)
        self.sent = sent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        item = self.script.pop(0)
        return item if isinstance(item, bytes) else json.dumps(item, ensure_ascii=False)


def _event(name, **fields):
    header = {'task_id': 't', 'event': name}
    header.update(fields)
    return {'header': header, 'payload': fields.get('payload', {})}


def _sentence(text, end=True):
    return _event('result-generated',
                  payload={'output': {'sentence': {'text': text,
                                                   'sentence_end': end,
                                                   'heartbeat': False}}})


class CloudProtocolTests(unittest.TestCase):
    """云端 Paraformer 的协议解析：报文、事件取舍、失败传播。

    协议是 2026-11-16 按官方文档对的，错一处就是"转写全是空的"或"连不上却
    报成功"——所以这里用假 socket 把四个关键点钉住。
    """

    def _run(self, script):
        import asyncio
        import wave
        import tempfile
        import unittest.mock as mock

        sent = []
        fake = FakeWebSocket(script, sent)

        async def fake_connect(*args, **kwargs):
            return fake

        # `async with websockets.connect(...)`：connect 的返回值必须是异步上下文
        # 管理器（websockets≥14 的 Connect 两者都是），所以 mock 直接返回假 socket
        # 而不是协程——协程没有 __aenter__。
        def sync_connect(*args, **kwargs):
            return fake

        # 造一个 16kHz 单声道 16bit 的短 WAV（协议要求 format=pcm，采样率从文件头读）
        tmp = tempfile.mkdtemp(prefix='wv-test-')
        wav_path = os.path.join(tmp, 'a.wav')
        with wave.open(wav_path, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b'\x00\x00' * 1600)
        try:
            with mock.patch('websockets.connect', sync_connect):
                return wv.cloud_transcribe_wav(wav_path, 'sk-test'), sent
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_只收句尾结果_中间结果与心跳不计入(self):
        text, sent = self._run([
            _event('task-started'),
            _sentence('第一句中间态', end=False),      # 中间结果：会被下一句覆盖
            _event('result-generated', payload={'output': {'sentence': {
                'text': '', 'heartbeat': True, 'sentence_end': False}}}),  # 心跳：跳过
            _sentence('第一句'),
            _sentence('第二句'),
            _event('task-finished'),
        ])
        self.assertEqual(text, '第一句第二句')

    def test_run_task报文形状(self):
        _text, sent = self._run([_event('task-started'), _event('task-finished')])
        texts = [s for s in sent if isinstance(s, str)]
        run_task = json.loads(texts[0])
        header = run_task['header']
        self.assertEqual(header['action'], 'run-task')
        self.assertEqual(header['streaming'], 'duplex')
        payload = run_task['payload']
        self.assertEqual(payload['model'], 'paraformer-realtime-v2')
        self.assertEqual(payload['parameters']['format'], 'pcm')
        self.assertEqual(payload['parameters']['sample_rate'], 16000)
        self.assertEqual(payload['parameters']['language_hints'], ['zh', 'yue'])

    def test_服务端失败要抛_不静默当空(self):
        with self.assertRaises(RuntimeError):
            self._run([_event('task-started'),
                       _event('task-failed', error_code='CLIENT_ERROR',
                              error_message='request timeout')])

    def test_finish_task在音频之后发(self):
        _text, sent = self._run([_event('task-started'), _event('task-finished')])
        texts = [s for s in sent if isinstance(s, str)]
        self.assertEqual(json.loads(texts[-1])['header']['action'], 'finish-task')
        # 音频是二进制帧，排在两条文本指令之间
        self.assertTrue(any(isinstance(s, (bytes, bytearray)) for s in sent),
                        'PCM 必须以二进制帧发送')


class _FakeHTTPResponse:
    """urlopen 的返回值形状：上下文管理器 + read()。"""

    def __init__(self, payload, status=200):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode('utf-8')


class MiniMaxProtocolTests(unittest.TestCase):
    """MiniMax speech_to_text 的报文与响应解析（2026-11-16 按官方文档）。

    这条路的形状与 DashScope 完全不同：一次 multipart 表单直传本地文件。
    错一处（字段名、header、响应路径）的表现是"转写全是空的"，所以用假
    urllib 把请求与响应两头都钉住。
    """

    def _wav(self):
        import tempfile
        import wave
        tmp = tempfile.mkdtemp(prefix='wv-mm-')
        path = os.path.join(tmp, 'a.wav')
        with wave.open(path, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b'\x00\x01' * 800)
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        return path

    def _call(self, payload, status=200, language='zh'):
        import unittest.mock as mock
        import urllib.error
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['url'] = request.full_url
            captured['headers'] = dict(request.header_items())
            captured['body'] = request.data
            if status and status >= 400:
                raise urllib.error.HTTPError(request.full_url, status, 'err', None, None)
            return _FakeHTTPResponse(payload)

        with mock.patch('urllib.request.urlopen', fake_urlopen):
            if status and status >= 400:
                with self.assertRaises(RuntimeError):
                    wv.minimax_transcribe_wav(self._wav(), 'mm-key', language=language)
            else:
                text = wv.minimax_transcribe_wav(self._wav(), 'mm-key', language=language)
                return text, captured
        return None, captured

    def test_取text字段(self):
        text, _ = self._call({'text': '实际上还是商家赚了', 'duration': 26.3,
                              'trace_id': 'abc'})
        self.assertEqual(text, '实际上还是商家赚了')

    def test_空文本要抛_带trace_id(self):
        """空文本可能是真静音也可能是别的问题——不静默当成功，
        trace_id 带上，排查时能对着服务端日志找到这一条。"""
        import unittest.mock as mock
        with mock.patch('urllib.request.urlopen',
                        lambda req, timeout=None: _FakeHTTPResponse(
                            {'text': '', 'trace_id': 't-1'})):
            with self.assertRaises(RuntimeError) as ctx:
                wv.minimax_transcribe_wav(self._wav(), 'mm-key')
        self.assertIn('t-1', str(ctx.exception))

    def test_端点与鉴权头(self):
        _text, captured = self._call({'text': '字'})
        self.assertEqual(captured['url'], 'https://api.minimax.cn/v1/speech_to_text')
        self.assertEqual(captured['headers'].get('Authorization'), 'Bearer mm-key')
        self.assertIn('multipart/form-data', captured['headers'].get('Content-type', ''))

    def test_multipart里四个部件齐整(self):
        _text, captured = self._call({'text': '字'}, language='yue')
        body = captured['body']
        self.assertIn(b'name="model"', body)
        self.assertIn(b'asr-1.0', body)
        self.assertIn(b'name="response_format"', body)
        self.assertIn(b'name="file"; filename="voice.wav"', body)
        self.assertIn(b'audio/wav', body)
        # 语言走 **header** 不是表单字段：两种传错一种的表现都是"用了默认语言"
        self.assertEqual(captured['headers'].get('Language'), 'yue')

    def test_HTTP错误要抛_不静默当空(self):
        _none, captured = self._call({}, status=401)
        # _call 内部已断言抛 RuntimeError；这里再确认它带了状态码
        import unittest.mock as mock
        import urllib.error
        import tempfile
        import wave
        tmp = tempfile.mkdtemp(prefix='wv-mm2-')
        path = os.path.join(tmp, 'a.wav')
        with wave.open(path, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b'\x00\x00' * 100)

        def raise_http(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 401, 'Unauthorized',
                                         None, None)

        with mock.patch('urllib.request.urlopen', raise_http):
            with self.assertRaises(RuntimeError) as ctx:
                wv.minimax_transcribe_wav(path, 'bad-key')
        self.assertIn('401', str(ctx.exception))
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()