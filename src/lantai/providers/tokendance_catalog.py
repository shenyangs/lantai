"""Release-owned capabilities; public TokenDance catalog, 2026-09-12.

Source: https://tokendance.space/gateway/v1/models
Only public protocol/context metadata is bundled, never credentials/pricing.
Review snapshot changes with the provider-state version and parity tests.
"""

import json

CATALOG = json.loads(
    r"""
{
  "minimax-h3-max": {
    "supported_protocols": [
      "minimax:video_generation_v2"
    ],
    "context_window": null
  },
  "glm-4.7": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 200000
  },
  "glm-5": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 200000
  },
  "qwen3-max": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 256000
  },
  "deepseek-v3.2": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 128000
  },
  "minimax-m2.5": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 200000
  },
  "kimi-k2.5": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 256000
  },
  "minimax-m2.7": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages",
      "openai:responses"
    ],
    "context_window": 200000
  },
  "glm-4.5-air": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 128000
  },
  "glm-4.6v": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 128000
  },
  "deepseek-ocr-2": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 8000
  },
  "step-3.5-flash": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 256000
  },
  "seed-2.0-pro": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 256000
  },
  "seed-2.0-lite": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 256000
  },
  "seed-2.0-mini": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 256000
  },
  "glm-5v-turbo": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 202752
  },
  "seedream-5.0-lite": {
    "supported_protocols": [
      "ark:image-generations",
      "openai:image-generations"
    ],
    "context_window": null
  },
  "qwen3.6-max-preview": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 262144
  },
  "glm-5.1": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 202752
  },
  "qwen3-vl-plus": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 256000
  },
  "glm-ocr": {
    "supported_protocols": [
      "zai:layout-parsing"
    ],
    "context_window": 128000
  },
  "kimi-k2.6": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 262144
  },
  "qwen-text-embedding-v4": {
    "supported_protocols": [
      "openai:embeddings"
    ],
    "context_window": 8000
  },
  "qwen3.5-plus": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 1000000
  },
  "qwen3.5-flash": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 1000000
  },
  "happyhorse-1.0-video-edit": {
    "supported_protocols": [
      "happyhorse:video-synthesis"
    ],
    "context_window": null
  },
  "happyhorse-1.0-i2v": {
    "supported_protocols": [
      "happyhorse:video-synthesis"
    ],
    "context_window": null
  },
  "happyhorse-1.0-t2v": {
    "supported_protocols": [
      "happyhorse:video-synthesis"
    ],
    "context_window": null
  },
  "happyhorse-1.0-r2v": {
    "supported_protocols": [
      "happyhorse:video-synthesis"
    ],
    "context_window": null
  },
  "deepseek-v4-pro": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages",
      "openai:responses"
    ],
    "context_window": 1048576
  },
  "qwen3.6-plus": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 1000000
  },
  "mimo-v2.5-pro": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages",
      "openai:responses"
    ],
    "context_window": 1048576
  },
  "unifuncs-s3": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 200000
  },
  "unifuncs-u3": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 200000
  },
  "seedance-2.0": {
    "supported_protocols": [
      "seedance:generations"
    ],
    "context_window": null
  },
  "mimo-v2.5": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages",
      "openai:responses"
    ],
    "context_window": 1048576
  },
  "seedance-2.0-fast": {
    "supported_protocols": [
      "seedance:generations"
    ],
    "context_window": null
  },
  "deepseek-chat-v3-0324": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 128000
  },
  "mimo-v2.5-tts": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 8000
  },
  "mimo-v2.5-tts-voicedesign": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 8000
  },
  "mimo-v2.5-tts-voiceclone": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 8000
  },
  "qwen3.7-plus": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages",
      "openai:responses"
    ],
    "context_window": 1000000
  },
  "qwen3.7-max": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages",
      "openai:responses"
    ],
    "context_window": 1000000
  },
  "minimax-speech-2.8-hd": {
    "supported_protocols": [
      "minimax:t2a_v2",
      "minimax:t2a_v2_ws",
      "minimax:voice_clone"
    ],
    "context_window": null
  },
  "minimax-speech-2.8-turbo": {
    "supported_protocols": [
      "minimax:t2a_v2",
      "minimax:t2a_v2_ws",
      "minimax:voice_clone"
    ],
    "context_window": null
  },
  "step-3.7-flash": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages",
      "openai:responses"
    ],
    "context_window": 256000
  },
  "bocha-web-search": {
    "supported_protocols": [
      "bocha:web-search"
    ],
    "context_window": null
  },
  "unifuncs-u3-pro": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 200000
  },
  "unifuncs-s3-pro": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 200000
  },
  "unifuncs-web-reader": {
    "supported_protocols": [
      "unifuncs:web-reader"
    ],
    "context_window": null
  },
  "unifuncs-web-search": {
    "supported_protocols": [
      "unifuncs:web-search"
    ],
    "context_window": null
  },
  "hy3-preview": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 256000
  },
  "kimi-k2.7-code": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 262144
  },
  "glm-5.2": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 1000000
  },
  "happyhorse-1.1-r2v": {
    "supported_protocols": [
      "happyhorse:video-synthesis"
    ],
    "context_window": null
  },
  "seed-evolving": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 256000
  },
  "seed-2.1-turbo": {
    "supported_protocols": [
      "openai:chat-completions",
      "openai:responses"
    ],
    "context_window": 256000
  },
  "minimax-m3": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages",
      "openai:responses"
    ],
    "context_window": 1000000
  },
  "seed-2.1-pro": {
    "supported_protocols": [
      "openai:chat-completions",
      "openai:responses"
    ],
    "context_window": 256000
  },
  "wan3.0-video": {
    "supported_protocols": [
      "wan3:video-synthesis"
    ],
    "context_window": null
  },
  "seedance-2.0-mini": {
    "supported_protocols": [
      "seedance:generations"
    ],
    "context_window": null
  },
  "qwen3.8-max": {
    "supported_protocols": [
      "openai:chat-completions",
      "openai:responses"
    ],
    "context_window": 1000000
  },
  "longcat-2.0": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 1000000
  },
  "hy3": {
    "supported_protocols": [
      "openai:chat-completions",
      "openai:responses"
    ],
    "context_window": 256000
  },
  "kling-3.0": {
    "supported_protocols": [
      "kling:text2video",
      "kling:image2video"
    ],
    "context_window": null
  },
  "happyhorse-1.1-t2v": {
    "supported_protocols": [
      "happyhorse:video-synthesis"
    ],
    "context_window": null
  },
  "kimi-k3": {
    "supported_protocols": [
      "openai:chat-completions",
      "openai:responses"
    ],
    "context_window": 1048576
  },
  "seedream-5.0-pro": {
    "supported_protocols": [
      "ark:image-generations"
    ],
    "context_window": null
  },
  "qwen3.5-35b-a3b": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 256000
  },
  "qwen3-30b-a3b-instruct-2507": {
    "supported_protocols": [
      "openai:chat-completions"
    ],
    "context_window": 128000
  },
  "deepseek-v4-flash-0731": {
    "supported_protocols": [
      "openai:chat-completions",
      "openai:responses",
      "anthropic:messages"
    ],
    "context_window": 1048576
  },
  "deepseek-v4-flash": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages",
      "openai:responses"
    ],
    "context_window": 1048576
  },
  "happyhorse-1.1-i2v": {
    "supported_protocols": [
      "happyhorse:video-synthesis"
    ],
    "context_window": null
  },
  "minimax-h3": {
    "supported_protocols": [
      "minimax:video_generation_v2"
    ],
    "context_window": null
  },
  "seedance-2.5": {
    "supported_protocols": [
      "seedance:generations"
    ],
    "context_window": null
  },
  "deepseek-v4-flash-vision-exp": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages",
      "openai:responses"
    ],
    "context_window": 1000000
  },
  "wan3.0-video-prime": {
    "supported_protocols": [
      "wan3:video-synthesis"
    ],
    "context_window": null
  },
  "glm-5.3": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 1000000
  },
  "dots-3-note-preview": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 512000
  },
  "seed-2.0-code": {
    "supported_protocols": [
      "openai:chat-completions",
      "openai:responses"
    ],
    "context_window": 256000
  },
  "ling-3.0-flash": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 256000
  },
  "deepseek-v4-pro-0813": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages",
      "openai:responses"
    ],
    "context_window": 1000000
  },
  "glm-5.3-flash": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 1000000
  },
  "kling-3.0-turbo": {
    "supported_protocols": [
      "kling:text2video",
      "kling:image2video"
    ],
    "context_window": null
  },
  "seed-tts-2.0": {
    "supported_protocols": [
      "ark:tts",
      "ark:tts_ws"
    ],
    "context_window": null
  },
  "hy4-preview": {
    "supported_protocols": [
      "openai:chat-completions",
      "openai:responses"
    ],
    "context_window": 1024000
  },
  "qwen3.7-text-embedding": {
    "supported_protocols": [
      "openai:embeddings"
    ],
    "context_window": 128000
  },
  "kling-3.0-omni": {
    "supported_protocols": [
      "kling:omni-video"
    ],
    "context_window": null
  },
  "qwen3.8-flash": {
    "supported_protocols": [
      "openai:chat-completions",
      "openai:responses",
      "anthropic:messages"
    ],
    "context_window": 1000000
  },
  "qwen3.8-max-0902": {
    "supported_protocols": [
      "openai:chat-completions",
      "openai:responses"
    ],
    "context_window": 1000000
  },
  "spark-x2.5-1.7b": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 1000000
  },
  "spark-x2.5-4b": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages"
    ],
    "context_window": 1000000
  },
  "deepseek-v4.1-flash": {
    "supported_protocols": [
      "openai:chat-completions",
      "anthropic:messages",
      "openai:responses"
    ],
    "context_window": 1000000
  }
}
"""
)
