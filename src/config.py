"""Configuration management for Community Scout.

Reads all tuneable values from environment variables at runtime.
No credential helpers or additional AWS infrastructure required for v0.1.
"""

import os


DEFAULT_BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-20250514"


def get_model_id() -> str:
    """Get the Bedrock model ID from environment or use default.
    
    The model ID is read at handler invocation time, allowing it to be changed
    without restarting the runtime.
    
    Returns:
        str: The Bedrock model ID to use for query interpretation and response formatting.
        
    Notes:
        - Any Bedrock model ID supporting tool use may be used.
        - Defaults to us.anthropic.claude-sonnet-4-20250514 if BEDROCK_MODEL_ID is not set.
    """
    return os.environ.get("BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL_ID)
