"""Pinned cl100k_base token estimates without a first-use network download."""
import base64
from functools import lru_cache
import hashlib
from pathlib import Path

import tiktoken


ASSET = Path(__file__).parent / "assets" / "cl100k_base.tiktoken"
SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"


@lru_cache(maxsize=1)
def get_encoding(name="cl100k_base"):
    if name != "cl100k_base":
        raise ValueError("Only the bundled cl100k_base tokenizer is supported.")
    contents = ASSET.read_bytes()
    if hashlib.sha256(contents).hexdigest() != SHA256:
        raise ValueError("Bundled tokenizer data is damaged. Restore src/assets/cl100k_base.tiktoken.")
    ranks = {base64.b64decode(token): int(rank)
             for token, rank in (line.split() for line in contents.splitlines() if line)}
    # Parameters from tiktoken 0.8.0's cl100k_base constructor (MIT; license in assets).
    return tiktoken.Encoding(
        name=name,
        pat_str=r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s",
        mergeable_ranks=ranks,
        special_tokens={"<|endoftext|>": 100257, "<|fim_prefix|>": 100258,
                        "<|fim_middle|>": 100259, "<|fim_suffix|>": 100260,
                        "<|endofprompt|>": 100276},
    )
