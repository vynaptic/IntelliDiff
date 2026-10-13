"""``ds_llm``: one in-process port to any configured model.

``profiles`` (YAML, host trust), ``policy`` (fail-closed egress), ``client`` (``LLMClient``:
budgets, retries, call log), ``probe`` (capability probe),
``usage`` (cost) and ``adapters`` (one module per wire dialect) over the ``model`` types.
"""
