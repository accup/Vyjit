import numpy as np
import sounddevice as sd

import socketio
from aiohttp import web
import jinja2
import aiohttp_jinja2

import importlib

import threading
import asyncio

import sys
import traceback

from _lib.analyzer import BaseAnalyzer
from _lib.util import numpy_to_bytes

from argparse import ArgumentParser, Namespace, ArgumentDefaultsHelpFormatter

from typing import Optional, Tuple, Dict, Sequence


class AnalyzerRoutine:
    def define_parser(self, parser: ArgumentParser):
        parser.add_argument(
            '--sample-rate', type=float,
            default=16000.0,
            help='sample rate of input signal in hertz',
        )
        parser.add_argument(
            '--channels', type=int,
            default=1,
            help='the number of signal channels',
        )
        parser.add_argument(
            '--default-time-step', type=int,
            default=2048,
            help='default signal clipping interval in samples',
        )
        parser.add_argument(
            '--default-window-size', type=int,
            default=2048,
            help='default signal clipping window size in samples',
        )
        parser.add_argument(
            '--skip', action='store_true',
            help='skip samples when the input data queue is full',
        )

    def setup(self, args: Namespace):
        self.sample_rate: float = args.sample_rate
        self.channels = args.channels

        self.indata_cache_lock = threading.Lock()
        self.indata_cache = np.zeros(
            (args.default_window_size, self.channels),
            dtype=np.float32,
        )

        self.skip: bool = args.skip
        self.queue_info_lock = asyncio.Lock()
        self.queue_info = {'get': 0, 'skip': 0}
        self.analyzer_dict: Dict[web.WebSocketResponse,
                                 Tuple[asyncio.Lock, BaseAnalyzer]] = dict()

    def _reshape_indata_cache(self, window_size: int):
        with self.indata_cache_lock:
            x = self.indata_cache
            y = np.zeros((window_size, self.channels), dtype=np.float32)
            w = min(x.shape[0], y.shape[0])
            y[y.shape[0] - w:, :] = x[x.shape[0] - w:, :]
            self.indata_cache = y

    def get_property(self, name: str):
        if name == 'window_size':
            with self.indata_cache_lock:
                return self.indata_cache.shape[0]
        else:
            raise ValueError("Unknown property name {!r}.".format(name))

    def set_property(self, name: str, value):
        if name == 'window_size':
            self._reshape_indata_cache(value)
        else:
            raise ValueError("Unknown property name {!r}.".format(name))

    async def _put_indata(self, indata: np.ndarray):
        async with self.queue_info_lock:
            try:
                self.indata_queue.put_nowait(indata)
            except asyncio.QueueFull:
                self.queue_info['skip'] += 1

    def _input_stream_callback(self, indata: np.ndarray, frames, time, status):
        with self.indata_cache_lock:
            x = indata
            y = self.indata_cache
            w = min(y.shape[0], x.shape[0])
            y[:y.shape[0] - w, :] = y[w:, :]
            y[y.shape[0] - w:, :] = x[x.shape[0] - w:, :]

            asyncio.run_coroutine_threadsafe(
                self._put_indata(np.copy(y)),
                loop=self.loop,
            )

    async def display_queue_info(self):
        while True:
            async with self.queue_info_lock:
                print(
                    '\r{} blocks queued, '
                    '{} blocks skipped, '
                    '{} blocks analyzed.'.format(
                        self.indata_queue.qsize(),
                        self.queue_info['skip'],
                        self.queue_info['get'],
                    ),
                    end='',
                    flush=True,
                )
                self.queue_info['get'] = 0
                self.queue_info['skip'] = 0
            await asyncio.sleep(2.0)

    async def analysis_coroutine(self):
        while True:
            indata = await self.indata_queue.get()
            if indata is None:
                break

            async with self.analyzer_dict_lock:
                items = list(self.analyzer_dict.items())

            for sid, (lock, analyzer) in items:
                try:
                    async with lock:
                        results = analyzer.analyze(indata)
                    results = numpy_to_bytes(results)
                    await self.sio.emit(
                        'results',
                        data=results,
                        room=sid,
                    )
                except KeyboardInterrupt:
                    raise
                except Exception:
                    await self.sio.emit(
                        'internal_error',
                        data=traceback.format_exc(),
                        room=sid,
                    )
                    await self.sio.disconnect(sid)

            async with self.queue_info_lock:
                self.queue_info['get'] += 1

    def main(self):
        try:
            asyncio.run(self.main_coroutine())
        except KeyboardInterrupt:
            pass

    async def main_coroutine(self):
        self.loop = asyncio.get_event_loop()
        self.indata_queue = asyncio.Queue(1 if self.skip else 0)
        self.analyzer_dict_lock = asyncio.Lock()

        from routes import routes
        app = web.Application()
        self.sio = socketio.AsyncServer(async_mode='aiohttp')
        # Require to attach firstly
        self.sio.attach(app)
        app.add_routes(routes)
        aiohttp_jinja2.setup(
            app,
            loader=jinja2.FileSystemLoader('_template'),
        )
        self.sio.on('start_analysis', self.handle_start_analysis)
        self.sio.on('disconnect', self.handle_disconnect)
        self.sio.on('set_properties', self.handle_set_properties)

        host = 'localhost'
        port = 8080
        print('Launch at http://{}:{}'.format(host, port))
        print('Press Ctrl+C to quit.')
        if self.skip:
            print('* Overflowed segments will be skipped.')

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                callback=self._input_stream_callback,
            ):
                await asyncio.gather(
                    self.display_queue_info(),
                    self.analysis_coroutine(),
                )
        finally:
            await runner.cleanup()

    async def handle_start_analysis(self, sid, name: str):
        analyzer_module_name = 'analyzers.{}'.format(name)
        analyzer_module = importlib.import_module(analyzer_module_name)
        analyzer = analyzer_module.Analyzer()
        data = analyzer.get_client_properties()

        async with self.analyzer_dict_lock:
            self.analyzer_dict[sid] = asyncio.Lock(), analyzer

        await self.sio.emit('properties', data, room=sid)

    async def handle_disconnect(self, sid):
        async with self.analyzer_dict_lock:
            self.analyzer_dict.pop(sid, None)

    async def handle_set_properties(
        self,
        sid,
        properties: dict,
    ):
        async with self.analyzer_dict_lock:
            if sid not in self.analyzer_dict:
                return
            lock, analyzer = self.analyzer_dict[sid]

        async with lock:
            for name, value in properties.items():
                if value is None:
                    continue
                setattr(
                    analyzer,
                    type(analyzer)._properties[name],
                    value,
                )

            data = analyzer.get_client_properties(properties.keys())
        await self.sio.emit('properties', data, room=sid)

    def run(self, command_line_args: Optional[Sequence[str]] = None):
        parser = ArgumentParser(
            prog=sys.argv[0],
            formatter_class=ArgumentDefaultsHelpFormatter,
        )
        self.define_parser(parser)
        args = parser.parse_args(command_line_args)

        self.setup(args)
        self.main()


if __name__ == '__main__':
    AnalyzerRoutine().run()
