import json
import re
import traceback
from asyncio import BoundedSemaphore, ensure_future, gather
from pathlib import Path
from typing import List, Tuple

from httpx import AsyncClient
from pydub import AudioSegment
from pydub.exceptions import CouldntDecodeError
from ratelimiter import RateLimiter


class WitAiAPI:
    """
    A class to interact with Wit.ai API
    Based on https://github.com/charslab/TranscriberBot work
    """

    def __init__(
        self, lang: str, semaphore: int, config_file: Path, verbose: bool = False
    ) -> None:
        self.api_keys = json.loads(config_file.read_text(encoding="utf-8"))
        self.api_url = "https://api.wit.ai"
        self.lang = lang
        self.chunks = 0
        self.processed_chunks = 0
        self._text = ""
        self.text_chunks: List[Tuple[int, str]] = []
        self._verbose = verbose
        self._sem = BoundedSemaphore(semaphore)
        if verbose:
            print(f"Simultaneous connection limit set to {semaphore}")

    @property
    def text(self) -> str:
        # text = "\n".join([i[1] for i in sorted(self.text_chunks, key=lambda x: x[0])])
        text = self._text
        if self.lang == "ar":
            text = (
                text.replace("?", "؟")
                .replace(" آآ ", "")
                .replace(" اه اه ", " ")
                .replace(" اه ", " ")
            )
        # strip extra white spaces from lines
        text = text.replace(".", ".\n").replace("\n ", "\n")
        text = re.sub(" {2,}", " ", text, re.M)
        return text

    def has_api_key(self) -> bool:
        return bool(self.api_keys.get(self.lang))

    @RateLimiter(max_calls=60, period=60)
    async def __transcribe_chunk(
        self, chunk: AudioSegment, idx: int, lang: str = "ar"
    ) -> Tuple[str, str]:
        """
        Based on https://github.com/charslab/TranscriberBot/blob/
        823b1423832b7117ad41c83abb3e25d58dd9e789/src/audiotools/
        speech.py#L13
        """
        text = ""
        error = ""
        headers = {
            "authorization": f"Bearer {self.api_keys[lang]}",
            "accept": "application/vnd.wit.20200513+json",
            "content-type": "audio/raw;encoding=signed-integer;bits=16;rate=8000;endian=little",
        }
        try:
            async with AsyncClient() as client:
                resp = await client.post(
                    f"{self.api_url}/speech",
                    headers=headers,
                    content=chunk.raw_data,
                    timeout=None,
                )
                response = resp.json()
                if resp.status_code == 200:
                    text = (
                        response["_text"] if "_text" in response else response["text"]
                    )
                else:
                    print(resp.status_code, response)
            self.processed_chunks += 1
            if self._verbose:
                print(f"Processed chunk {self.processed_chunks} of {self.chunks}")
                print(text)
        except Exception as e:
            error = f"Could not transcribe chunk: {e}\n{traceback.format_exc()}"
            print(f"Error in chunk {idx}:\n{error}")
        self.text_chunks.append((idx, text))
        return text, error

    @staticmethod
    async def __generate_chunks(
        segment: AudioSegment, length: float = 20000 / 1001
    ) -> List[AudioSegment]:
        """
        Based on https://github.com/charslab/TranscriberBot/blob/
        823b1423832b7117ad41c83abb3e25d58dd9e789/
        src/audiotools/speech.py#L49
        """
        return [
            segment[i : i + int(length * 1000)]
            for i in range(0, len(segment), int(length * 1000))
        ]

    @staticmethod
    async def __preprocess_audio(audio: AudioSegment) -> AudioSegment:
        """
        From https://github.com/charslab/TranscriberBot/blob/
        823b1423832b7117ad41c83abb3e25d58dd9e789/
        src/audiotools/speech.py#L67
        """
        return audio.set_sample_width(2).set_channels(1).set_frame_rate(8000)

    async def __bound_fetch(self, chunk: AudioSegment, idx: int) -> Tuple[str, str]:
        # Getter functions with semaphore.
        async with self._sem:
            return await self.__transcribe_chunk(chunk, idx)

    async def transcribe(self, path: Path) -> None:
        """
        Based on https://github.com/charslab/TranscriberBot/blob/
        823b1423832b7117ad41c83abb3e25d58dd9e789/
        src/audiotools/speech.py#L70
        """
        print(f"Transcribing file {path}")
        try:
            audio = AudioSegment.from_file(path)
            chunks = await self.__generate_chunks(await self.__preprocess_audio(audio))
            self.chunks = len(chunks)
            print(f"Got {len(chunks)} chunks")

            tasks = [
                ensure_future(self.__bound_fetch(chunk, idx))
                for idx, chunk in enumerate(chunks)
            ]
            results = await gather(*tasks)
            for result in results:
                if result and result[0]:
                    self._text += result[0]
        except CouldntDecodeError:
            raise Exception(
                "`Error decoding the audio file.\nEnsure that the provided audio is a valid audio file!`"
            )
