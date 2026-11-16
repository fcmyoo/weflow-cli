#!/usr/bin/env python3
"""Decode WeChat voice messages and transcribe them.

WeChat 4.x keeps voice payloads in `media_*.db` under `VoiceInfo.voice_data`,
encoded as SILK v3 (a one-byte length prefix, then `#!SILK_V3`). Browsers
cannot play SILK and ffmpeg has no SILK decoder, so a chat export has nothing
to show for a voice message; the audio is decoded with `pilk` and transcribed
with one of the engines below.

Recognition engines (`--engine auto|minimax|dashscope|local`):

- **MiniMax** (`asr-1.0`, preferred when its key is configured) - commercial
  ASR over a single multipart POST that takes the local file directly. The
  user picked this one over DashScope on 2026-11-16.
- **DashScope** (`paraformer-realtime-v2`) - commercial ASR over a WebSocket
  protocol. Kept as the second option: the two services get the same dialect
  clip wrong in different ways, and a switchable engine is one more chance
  not to quote a fabrication as fact.
- **Local** - `faster-whisper` on-device. Nothing leaves the machine. The
  default model is a Cantonese fine-tune, chosen because stock Whisper
  answers Cantonese with fluent-looking Mandarin nonsense. See OPERATIONS.md
  for how to fetch it. Slow and CPU-heavy: opt in explicitly.

All paths fill the same content-addressed cache, and the exporter only reads
it. That makes the pass resumable, re-runnable, and free to repeat once it
has been paid for once. Entries are namespaced per engine (`ENGINE_PREFIX`)
so a transcript's origin is always knowable - see `cache_key`.

Audio leaves the machine only on the cloud paths, and only when a key is
configured; keys are read from the CLI config, never from argv.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Appended to the transcript cache for a clip the recogniser returned nothing
# for, so a re-run does not pay for it twice.
EMPTY_MARKER = '\x00empty'

DEFAULT_MODEL = 'small'

# **转写缓存的家**，由这个模块定义，别处一律引用它。
#
# 2026-10-01 踩过一次：`chat_notes` 拿自己的输出目录（`output/chat-notes`）去拼缓存路径，于是
# 它读到的是 `output/chat-notes/.voice-cache` —— 一个刚被它自己建出来的空目录，而真正的缓存
# 在 `output/.voice-cache`（1,788 条）。它「命中 16 条」那个数字是自洽的，只是与真缓存不相通，
# 报告看不出来。位置只要有两份定义就会漂，所以从这里定一份。
# 可用 `WEFLOW_VOICE_CACHE` 覆盖（测试与自定义布局用）。
DEFAULT_CACHE_DIR = os.environ.get('WEFLOW_VOICE_CACHE') or os.path.join('output', '.voice-cache')

# A Cantonese-tuned model, fetched separately (see the README note in
# OPERATIONS.md). Worth preferring automatically when present: stock Whisper
# answers Cantonese speech with fluent-looking Mandarin nonsense - text that
# reads as a real sentence but says nothing that was said. A Cantonese
# fine-tune transcribes the same audio into actual Cantonese.
CANTONESE_MODEL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'models', 'whisper-small-cantonese', 'cts')

# Detected rather than forced. Forcing `yue` on the Cantonese model returned
# empty output for every clip tried; letting it detect produced Cantonese.
DEFAULT_LANGUAGE = None


def voice_shards(db_path):
    """`media_*.db` files that sit beside the configured message database."""
    db = os.path.abspath(db_path)
    folder = os.path.dirname(db)
    if not os.path.isdir(folder):
        return []
    return sorted(
        os.path.join(folder, name)
        for name in os.listdir(folder)
        if name.lower().startswith('media_') and name.lower().endswith('.db')
    )


def _open(path, key_hex, salt_hex, passphrase=''):
    """Connect to a shard, deriving its key the same way message shards do."""
    from export_chat_html import derive_database_key, connect
    shard_key, shard_salt = derive_database_key(path, key_hex, salt_hex, passphrase)
    conn, cursor = connect(path, shard_key, shard_salt)
    return conn, cursor


def load_voice_map(db_path, key_hex, salt_hex, passphrase='', talker=''):
    """{(talker, local_id): voice bytes} for one conversation.

    Scoped to a talker because a whole account is only ever needed one
    conversation at a time, and the payloads are held in memory.
    """
    voices = {}
    if not talker:
        return voices
    for shard in voice_shards(db_path):
        conn = None
        try:
            conn, cursor = _open(shard, key_hex, salt_hex, passphrase)
            cursor.execute('SELECT rowid, user_name FROM Name2Id')
            names = {row[0]: row[1] for row in cursor.fetchall()}
            cursor.execute(
                'SELECT chat_name_id, local_id, voice_data FROM VoiceInfo '
                'WHERE voice_data IS NOT NULL'
            )
            for chat_id, local_id, blob in cursor.fetchall():
                if names.get(chat_id) != talker or not blob:
                    continue
                voices[local_id] = bytes(blob)
        except Exception:
            continue
        finally:
            if conn is not None:
                conn.close()
    return voices


def voice_key(blob):
    """Cache key for a voice payload: its content digest."""
    return hashlib.md5(blob).hexdigest()


#: 转写引擎。`auto` 按下面的优先序挑；两个云端引擎都配了 key 时先 MiniMax
#: （用户在两家之间明确选了它；想换序用 `--engine` 或配置 `voiceAsrEngine`）。
ENGINES = ('auto', 'minimax', 'dashscope', 'local')
#: 引擎 → 缓存 key 前缀。**必须按引擎分**：不同引擎的文字质量不同，混在一个 key 下
#: 会让"这条转写是谁转的"不可知——而本机那条的失败模式是说得通顺但内容是编的。
#: 本地用裸 digest（老缓存不失效）。
ENGINE_PREFIX = {'dashscope': 'dashscope-', 'minimax': 'minimax-', 'local': ''}


def cache_key(blob, engine='local'):
    """Engine-namespaced cache key for a voice payload."""
    return ENGINE_PREFIX.get(engine, '') + voice_key(blob)


def lookup_transcript(cache, blob):
    """Read a cached transcript for `blob`, preferring cloud engines.

    Returns (text, engine); (None, '') when nothing is cached. Preference
    order is MiniMax, then DashScope, then local - all three beat nothing,
    and either cloud engine beats the local small model.
    """
    for engine in ('minimax', 'dashscope', 'local'):
        text = cache.get(cache_key(blob, engine))
        if text is not None:
            return text, engine
    return None, ''


def decode_silk(blob, wav_path):
    """Write `blob` to `wav_path` as 16kHz mono WAV. True on success.

    pilk takes paths rather than bytes, and the 16kHz rate is what Whisper
    wants, so no resampling step is needed afterwards.
    """
    try:
        import pilk
    except ImportError:
        return False
    silk_path = wav_path + '.silk'
    try:
        with open(silk_path, 'wb') as handle:
            handle.write(blob)
        pilk.silk_to_wav(silk_path, wav_path, rate=16000)
        return os.path.isfile(wav_path) and os.path.getsize(wav_path) > 44
    except Exception:
        return False
    finally:
        try:
            os.remove(silk_path)
        except OSError:
            pass


def resolve_model(model_name=''):
    """Model to load: the caller's choice, else a local Cantonese fine-tune,
    else the stock model name."""
    if model_name:
        return model_name
    if os.path.isdir(CANTONESE_MODEL_DIR):
        return CANTONESE_MODEL_DIR
    return DEFAULT_MODEL


def _prepare_cuda_dlls():
    """Put the pip-provided CUDA libraries on PATH.

    ctranslate2 resolves cuBLAS/cuDNN through PATH on Windows and ignores
    `os.add_dll_directory`, so the `nvidia-*` wheel directories have to be
    prepended before it is imported. Without this, a `cuda` request fails with
    "Library cublas64_12.dll is not found" even when the wheels are installed.
    """
    try:
        import glob
        import sysconfig
    except ImportError:
        return
    if os.name != 'nt':
        return
    site = sysconfig.get_paths().get('purelib', '')
    if not site:
        return
    dirs = glob.glob(os.path.join(site, 'nvidia', '*', 'bin'))
    if dirs:
        os.environ['PATH'] = os.pathsep.join(dirs) + os.pathsep + os.environ.get('PATH', '')


def load_model(model_name='', device='auto'):
    """A Whisper model, or None when faster-whisper is unavailable.

    `auto` prefers the GPU: on the tested hardware a clip goes from 2.0s to
    0.07s, which is the difference between an overnight pass and a coffee
    break. Falls back to CPU whenever CUDA is not usable, so a machine without
    the GPU libraries still works, just slower.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    order = ('cuda', 'cpu') if device in ('auto', 'cuda') else ('cpu',)
    for candidate in order:
        if candidate == 'cuda':
            _prepare_cuda_dlls()
        try:
            compute = 'float16' if candidate == 'cuda' else 'int8'
            return WhisperModel(resolve_model(model_name), device=candidate, compute_type=compute)
        except Exception:
            continue
    return None


class TranscriptCache:
    """Content-addressed transcripts on disk.

    Keyed by the voice payload's digest rather than by message id: the same
    clip forwarded between chats is recognised once, and a re-export after the
    message ids shift still hits.
    """

    def __init__(self, cache_dir):
        self.dir = cache_dir or ''

    def path(self, key):
        return os.path.join(self.dir, key) if self.dir else ''

    def get(self, key):
        """Transcript, or None when this clip has not been processed yet."""
        path = self.path(key)
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                text = handle.read()
        except OSError:
            return None
        return '' if text == EMPTY_MARKER else text

    def put(self, key, text):
        path = self.path(key)
        if not path:
            return
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(text if text else EMPTY_MARKER)
        except OSError:
            pass


def transcribe_wav(wav_path, model, beam_size=1, language=DEFAULT_LANGUAGE):
    """Transcript for a WAV file, or '' when nothing was recognised.

    `condition_on_previous_text=False` 是**针对复读**的：Whisper 会把已生成的文本当作
    后续解码的上文条件，在听不清的时候这个条件会把同一句反复"确认"下去（实测同一段
    方言语音稳定输出「我做的事情都没有做过」三遍）。关掉它以后每段独立解码，复读的
    自我强化链就断了。`vad_filter` 再切掉静音——短语音里静音占比高时最容易触发幻觉。
    """
    try:
        segments, _ = model.transcribe(
            wav_path, language=language, beam_size=beam_size,
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters={'min_silence_duration_ms': 300},
            no_speech_threshold=0.6,
            # 复读的第二道闸：`compression_ratio_threshold` 判"这段输出是不是在重复自己"，
            # 超了就提高温度重解（默认 2.4，实测偏松）；`temperature` 给一条回退阶梯，
            # 让它在重复时真的敢走下一步而不是原地复读。
            compression_ratio_threshold=2.0,
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            repetition_penalty=1.15,
        )
        return ''.join(segment.text for segment in segments).strip()
    except TypeError:
        # 老版本 faster-whisper 不认这些关键字：退回最朴素的调用，别因为参数报错丢掉转写。
        try:
            segments, _ = model.transcribe(wav_path, language=language, beam_size=beam_size)
            return ''.join(segment.text for segment in segments).strip()
        except Exception:
            return ''
    except Exception:
        return ''


# ============================================================================
# 云端转写（DashScope Paraformer 实时语音识别）
#
# 为什么有这条路：本机 faster-whisper 的 small 模型在方言/粤语上会把话说得
# "像真的"——读起来是通顺的中文，内容却是编的（stock Whisper 对粤语的已知失败
# 模式）。那种错比空结果更贵：它会进知识卡、进概念页，被当成事实引用。
# Paraformer-realtime-v2 是阿里百炼的语音识别服务，中文/粤语的实测质量明显更好。
#
# 协议（2026-11-16 按官方文档核对，勿凭记忆改）：
#   wss://dashscope.aliyuncs.com/api-ws/v1/inference，握手头 Authorization: Bearer <key>
#   1) 发 run-task（JSON 文本帧）→ 等 task-started
#   2) 发二进制音频帧（16kHz 单声道 PCM，约 100ms 一块）
#   3) 收 result-generated 事件：payload.output.sentence.text，
#      sentence_end=true 的才是最终结果，中间结果会被下一句覆盖
#   4) 发 finish-task → 收 task-finished
#   失败时服务端发 task-failed（header.error_code / error_message）并关闭连接。
# 非实时那套 REST API 只收公网 URL、明确不支持 base64 与本地流，所以不走。
# ============================================================================

# ============================================================================
# 云端转写（MiniMax speech_to_text，asr-1.0）
#
# 与 DashScope 那条并存，按 key 自动挑（都配了先 MiniMax，见 ENGINES 的注释）。
# 为什么值得有第二条：两家对同一条方言语音的错法不同，而本机 small 模型的错
# 是"说得通顺但内容是编的"——多一个可切换的商用引擎，就多一次不把编造当事实
# 的机会。协议（2026-11-16 按官方文档核对，勿凭记忆改）：
#   POST https://api.minimax.cn/v1/speech_to_text
#   Authorization: Bearer <key>
#   multipart/form-data：model=asr-1.0、file=@<本地文件>、response_format=json
#   请求头 language：BCP-47 单值（zh / yue / en / ja ...），不传=混合语言识别
#   响应 {"text": "...", "duration": 秒, "trace_id": "..."}
# 约束：≤500 秒、≤50MB、**不支持裸 PCM**（我们的 16kHz 单声道 wav 正合适）。
# 比 DashScope 简单得多：一次 HTTP 表单直传本地文件，没有 websocket 状态机，
# 所以这条路用 stdlib urllib 就够，不加依赖。
# ============================================================================

MINIMAX_ASR_ENDPOINT = 'https://api.minimax.cn/v1/speech_to_text'
MINIMAX_ASR_MODEL = 'asr-1.0'
#: 语言头默认值。给 zh 而不是留空（混合模式）：实测混合识别在短句上不如显式
#: 单语种稳。粤语为主的会话用 --cloud-language yue 覆盖。
MINIMAX_LANGUAGE = 'zh'


def minimax_transcribe_wav(wav_path, key, model=MINIMAX_ASR_MODEL,
                           language=MINIMAX_LANGUAGE, timeout=120, attempts=2):
    """一个 WAV → 文字（MiniMax）。失败抛异常，调用方计数，**不静默当空**。

    重试只覆盖连接层（超时/连接错误）；HTTP 4xx 是参数或 key 的问题，
    重试只是把同一个错误重复两遍，所以解析出状态码后直接抛。
    """
    import urllib.error
    import urllib.request

    boundary = '----weflowVoice%s' % hashlib.md5(wav_path.encode()).hexdigest()
    with open(wav_path, 'rb') as handle:
        audio = handle.read()

    def field(name, value):
        # 每个字段自带 `--boundary` 前缀，所以 body **不要**再另起一个——
        # 2026-11-16 真机实测：多加一个会让开头变成 `--b\r\n--b\r\n`（一个畸形
        # 空 part），服务端报 400 "Error when parsing request"。
        return (b'--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                % (boundary.encode(), name.encode(), str(value).encode()))

    #: 文件 part 的分隔符。**必须单独 % 替换**：2026-11-16 真机实测，把它并进
    # 下面那串 `+` 拼接时，`%` 只作用于最后一个字面量，file part 的分隔符会以
    # 字面 `--%s` 留在 body 里——服务端报 400 "Error when parsing request"，
    # 而 `assertIn(b'name="file"')` 照样中（畸形分隔符不删内容）。
    file_header = (b'--%s\r\nContent-Disposition: form-data; name="file"; '
                   b'filename="voice.wav"\r\nContent-Type: audio/wav\r\n\r\n'
                   % boundary.encode())

    body = (field('model', model)
            + field('response_format', 'json')
            + file_header
            + audio + b'\r\n'
            + b'--%s--\r\n' % boundary.encode())

    last = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            MINIMAX_ASR_ENDPOINT, data=body, method='POST',
            headers={'Authorization': 'Bearer ' + key,
                     'Content-Type': 'multipart/form-data; boundary=%s' % boundary,
                     **({'language': language} if language else {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode('utf-8'))
            text = (payload.get('text') or '').strip()
            if not text:
                # 空文本可能是真的静音，也可能是别的问题——trace_id 带上，
                # 排查时对着服务端日志能找到这一条。
                raise RuntimeError('MiniMax 返回空文本（trace_id=%s）'
                                   % payload.get('trace_id'))
            return text
        except urllib.error.HTTPError as error:
            detail = ''
            try:
                detail = error.read().decode('utf-8', 'replace')[:200]
            except Exception:
                pass
            raise RuntimeError('MiniMax HTTP %s: %s' % (error.code, detail))
        except (ConnectionError, TimeoutError, OSError) as error:
            last = error
    raise last

CLOUD_ASR_ENDPOINT = 'wss://dashscope.aliyuncs.com/api-ws/v1/inference'
CLOUD_ASR_MODEL = 'paraformer-realtime-v2'
#: 16kHz 16bit 单声道下约 100ms 的字节数（官方建议每块 1KB–16KB / 约 100ms）
CLOUD_CHUNK_BYTES = 3200
#: 语言提示。默认同时给普通话与粤语：本仓的用户语音两种都有，而 language_hints
#: 不给时模型自动判断——实测自动判断在短句上不如显式提示稳。可用 --hints 覆盖。
CLOUD_LANGUAGE_HINTS = ['zh', 'yue']


def cloud_available(key: str) -> bool:
    """云端转写可用 = 有 key（协议与服务端无需任何本地依赖）。"""
    return bool(key)


def cloud_transcribe_wav(wav_path, key, model=CLOUD_ASR_MODEL,
                         language_hints=None, timeout=90, attempts=2):
    """一个 WAV → 文字。失败抛异常（调用方计数，**不静默当空**）。

    重试只覆盖连接层（超时/连接错误/服务端 task-failed）：限流与偶发抖动
    重试一次就好，而"这条语音永远没字"读的人不会知道。参数错误（4xx 类）
    重试只是把同一个错误重复两遍，所以 attempts 保持很小。
    """
    import asyncio
    import uuid
    import wave
    import websockets

    hints = list(language_hints or CLOUD_LANGUAGE_HINTS)
    with wave.open(wav_path, 'rb') as wav:
        sample_rate = wav.getframerate()
        pcm = wav.readframes(wav.getnframes())
    if not pcm:
        return ''

    async def run_once():
        task_id = str(uuid.uuid4())
        sentences = []
        async with websockets.connect(
                CLOUD_ASR_ENDPOINT,
                additional_headers={'Authorization': 'Bearer ' + key},
                open_timeout=timeout, close_timeout=10) as ws:
            await ws.send(json.dumps({
                'header': {'action': 'run-task', 'task_id': task_id,
                           'streaming': 'duplex'},
                'payload': {
                    'task_group': 'audio', 'task': 'asr',
                    'function': 'recognition', 'model': model, 'input': {},
                    'parameters': {'format': 'pcm', 'sample_rate': sample_rate,
                                   'language_hints': hints},
                },
            }))
            started = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if (started.get('header') or {}).get('event') != 'task-started':
                raise RuntimeError('云端 ASR 未启动：%s' % json.dumps(started, ensure_ascii=False)[:200])

            for offset in range(0, len(pcm), CLOUD_CHUNK_BYTES):
                await ws.send(pcm[offset:offset + CLOUD_CHUNK_BYTES])
            await ws.send(json.dumps({
                'header': {'action': 'finish-task', 'task_id': task_id},
                'payload': {'input': {}},
            }))

            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout)
                if isinstance(raw, bytes):
                    continue
                event = json.loads(raw)
                header = event.get('header') or {}
                if header.get('event') == 'task-failed':
                    raise RuntimeError('云端 ASR 失败：%s %s'
                                       % (header.get('error_code'),
                                          header.get('error_message')))
                if header.get('event') == 'task-finished':
                    break
                sentence = ((event.get('payload') or {}).get('output') or {}).get('sentence') or {}
                if sentence.get('sentence_end') and sentence.get('text'):
                    sentences.append(sentence['text'])
        return ''.join(sentences).strip()

    #: 值得重试的只有连接层：超时、连接中断、websocket 握手/协议异常。
    #: 协议层失败（task-failed、非 task-started 应答，都是 RuntimeError）不在内——
    #: 那是确定性错误，重试只是把同一个答案重复两遍，还会拖慢整条队列。
    import websockets
    retryable = (ConnectionError, TimeoutError, OSError,
                 getattr(websockets, 'WebSocketException', ()))

    last = None
    for attempt in range(1, attempts + 1):
        try:
            return asyncio.run(run_once())
        except retryable as error:
            last = error
    raise last


def resolve_engine(engine, minimax_key='', dashscope_key=''):
    """'auto' → 按 key 挑引擎：先 MiniMax，再 DashScope，都没有才本地。

    返回 (engine, 原因)。云端优先是因为本机 whisper-small 会把方言/粤语
    说得"像真的"（见模块头）；两家云端之间先 MiniMax 是用户在两家之间
    明确选了它（2026-11-16），想换序用 `--engine` 显式指定。
    """
    if engine != 'auto':
        return engine, '命令行/配置指定'
    if minimax_key:
        return 'minimax', '检测到 minimaxApiKey'
    if dashscope_key:
        return 'dashscope', '检测到 dashscopeApiKey'
    return 'local', '两个云端 key 都没配，退回本机 Whisper'


#: `--engine` 的旧值 'cloud' 曾是 DashScope 的代号，留着当别名，别让老命令报错。
ENGINE_ALIASES = {'cloud': 'dashscope'}


def normalize_engine(engine):
    return ENGINE_ALIASES.get(engine, engine)


def ensure_transcripts(voices, cache_dir, model_name='',
                       beam_size=1, limit=0, language=DEFAULT_LANGUAGE,
                       device='auto', log=print, engine='auto',
                       minimax_key='', dashscope_key='', language_hints=None,
                       cloud_language=MINIMAX_LANGUAGE):
    """Fill the transcript cache for any clip that is missing one.

    Returns (processed, cached, failed). Pending work is measured by cache
    misses, so an interrupted run resumes where it stopped instead of starting
    over - which matters, because a long conversation is hours of audio.

    `engine`：'auto'（默认）/ 'minimax' / 'dashscope' / 'local'，见 `resolve_engine`。
    两个 key 都由 `_utils` 解密后传入；`language_hints` 给 DashScope（多值），
    `cloud_language` 给 MiniMax（单值 BCP-47）。

    **缓存按引擎分命名空间**（`ENGINE_PREFIX`）：不同引擎的文字质量不同——商用
    ASR 与 small 模型加"未校对"标注不是一回事——混在一个 key 下会让"一条转写
    是谁转的"不可知。
    """
    cache = TranscriptCache(cache_dir)
    resolved_engine, why = resolve_engine(engine, minimax_key, dashscope_key)
    prefix = ENGINE_PREFIX.get(resolved_engine, '')
    pending = []
    for local_id, blob in voices.items():
        if not blob:
            continue
        key = prefix + voice_key(blob)
        if cache.get(key) is None:
            pending.append((key, blob))
    # Count the cache hits before --limit trims the queue, otherwise a limited
    # run reports everything it did not process as already done.
    already = len(voices) - len(pending)
    if limit and limit > 0:
        pending = pending[:limit]
    if not pending:
        log(f'所有 {len(voices)} 条语音都已有转写（缓存命中）')
        return 0, already, 0

    model = None
    cloud_key = ''
    if resolved_engine == 'minimax':
        if not minimax_key:
            # 不静默跳过：调用方看到的是"0 条新转写"，与"全部命中缓存"分不开。
            log(f'指定了 MiniMax 转写但没有 minimaxApiKey（weflow-cli config set '
                f'minimaxApiKey "..."），{len(pending)} 条未转写')
            return 0, already, len(pending)
        cloud_key = minimax_key
        log(f'待转写 {len(pending)} 条（已有 {already} 条）')
        log(f'引擎: MiniMax {MINIMAX_ASR_MODEL}（{why}；language={cloud_language or "混合"}）')
    elif resolved_engine == 'dashscope':
        if not dashscope_key:
            log(f'指定了 DashScope 转写但没有 dashscopeApiKey（weflow-cli config set '
                f'dashscopeApiKey "..."），{len(pending)} 条未转写')
            return 0, already, len(pending)
        cloud_key = dashscope_key
        log(f'待转写 {len(pending)} 条（已有 {already} 条）')
        log(f'引擎: DashScope {CLOUD_ASR_MODEL}（{why}）')
        log(f'语言提示: {"、".join(language_hints or CLOUD_LANGUAGE_HINTS)}'
            f'（改不了的话用 --hints）')
    else:
        resolved = resolve_model(model_name)
        log(f'待转写 {len(pending)} 条（已有 {already} 条）')
        log(f'模型: {resolved}（{why}；方言/粤语的错法是把话说得通顺但内容不对，'
            f'结果按"未校对"用——要商用质量配 minimaxApiKey 或 dashscopeApiKey）')
        model = load_model(model_name, device=device)
        if model is None:
            log('无法加载 faster-whisper 模型，转写跳过')
            return 0, already, len(pending)

    import tempfile
    processed = failed = 0
    scratch = tempfile.mkdtemp(prefix='weflow-voice-')
    try:
        for index, (key, blob) in enumerate(pending, 1):
            wav_path = os.path.join(scratch, f'{index}.wav')
            if not decode_silk(blob, wav_path):
                cache.put(key, '')
                failed += 1
            else:
                try:
                    if resolved_engine == 'minimax':
                        text = minimax_transcribe_wav(wav_path, cloud_key,
                                                      language=cloud_language)
                    elif resolved_engine == 'dashscope':
                        text = cloud_transcribe_wav(wav_path, cloud_key,
                                                    language_hints=language_hints)
                    else:
                        text = transcribe_wav(wav_path, model, beam_size=beam_size,
                                              language=language)
                except Exception as error:  # noqa: BLE001 - 网络失败计 failed，不写缓存
                    log(f'  第 {index} 条转写失败：{str(error)[:120]}')
                    failed += 1
                    try:
                        os.remove(wav_path)
                    except OSError:
                        pass
                    continue
                cache.put(key, text)
                processed += 1
                try:
                    os.remove(wav_path)
                except OSError:
                    pass
            if index % 10 == 0 or index == len(pending):
                log(f'  转写 {index}/{len(pending)}（新增 {processed}，失败 {failed}）')
    finally:
        try:
            import shutil
            shutil.rmtree(scratch, ignore_errors=True)
        except Exception:
            pass
    return processed, already, failed


def _main():
    import argparse
    parser = argparse.ArgumentParser(
        description='微信语音转写（MiniMax / DashScope 云端 ASR 或本机 Whisper）')
    parser.add_argument('--db', required=True, help='message_0.db 路径，用于定位同目录的 media_*.db')
    parser.add_argument('--key', required=True, help='NT key hex')
    parser.add_argument('--salt', required=True, help='NT salt hex')
    parser.add_argument('--passphrase', default='', help='分片密钥口令')
    parser.add_argument('--talker', required=True, help='会话 id')
    parser.add_argument('--cache-dir', required=True, help='转写缓存目录')
    parser.add_argument('--model', default='', help='模型名或本地目录（默认优先用本地粤语模型）')
    parser.add_argument('--language', default='', help='语言代码；留空为自动检测（粤语模型必须留空）')
    parser.add_argument('--device', default='auto', choices=['auto', 'cuda', 'cpu'], help='推理设备')
    parser.add_argument('--beam-size', type=int, default=1)
    parser.add_argument('--limit', type=int, default=0, help='本次最多转写多少条（0 为不限）')
    parser.add_argument('--engine', default='auto',
                        choices=['auto', 'minimax', 'dashscope', 'cloud', 'local'],
                        help='转写引擎：auto=按 key 挑，先 MiniMax 再 DashScope，都没有才本地（默认）；'
                             'minimax/dashscope/cloud=强制某家云端（没 key 会说明）；local=强制本机')
    parser.add_argument('--hints', default='',
                        help='DashScope 语言提示，逗号分隔（默认 zh,yue；可英文 en、日文 ja 等）')
    parser.add_argument('--cloud-language', default='',
                        help='MiniMax 语言头（默认 zh；粤语为主的会话用 yue；留空=混合识别）')
    args = parser.parse_args()

    # 云端 key 从配置解密后传入，**不走命令行参数**：密钥不该出现在进程表与 shell 历史里
    minimax_key = dashscope_key = ''
    try:
        from _utils import get_dashscope_key, get_minimax_key, load_config
        config = load_config()
        minimax_key = get_minimax_key(config)
        dashscope_key = get_dashscope_key(config)
    except Exception:
        pass

    voices = load_voice_map(args.db, args.key, args.salt, args.passphrase, args.talker)
    print(f'会话 {args.talker}: 找到 {len(voices)} 条语音', flush=True)
    hints = [h.strip() for h in args.hints.split(',') if h.strip()] or None
    processed, already, failed = ensure_transcripts(
        voices, args.cache_dir, model_name=args.model,
        beam_size=args.beam_size, limit=args.limit,
        language=args.language or None, device=args.device,
        engine=normalize_engine(args.engine),
        minimax_key=minimax_key, dashscope_key=dashscope_key,
        language_hints=hints, cloud_language=args.cloud_language or MINIMAX_LANGUAGE)
    print(json.dumps({'success': True, 'voices': len(voices),
                      'transcribed': processed, 'cached': already, 'failed': failed},
                     ensure_ascii=False))


if __name__ == '__main__':
    _main()
