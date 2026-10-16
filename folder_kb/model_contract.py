"""Plugin-declared model wiring; configuration checks never contact a provider."""
import re
from typing import Literal

from pydantic import Field, model_validator

from .errors import KnowledgeError
from .inbox import StrictModel


class ModelBinding(StrictModel):
    required_when: dict[str, str] = Field(default_factory=dict, max_length=8)
    environment: dict[str, Literal['base_url', 'model', 'api_key']] = Field(min_length=2, max_length=12)
    required_fields: list[Literal['base_url', 'model', 'api_key']] = Field(default_factory=lambda: ['base_url', 'model'], max_length=3)

    @model_validator(mode='after')
    def validate_binding(self):
        if any(not re.fullmatch(r'[A-Z][A-Z0-9_]{0,48}_(BASE_URL|MODEL|API_KEY)', name) for name in self.environment):
            raise ValueError('Model environment names must use the supported suffixes')
        if not {'base_url', 'model', *self.required_fields}.issubset(self.environment.values()):
            raise ValueError('Declare mappings for base_url, model and required fields')
        if any(not key or len(key) > 100 or len(value) > 100 for key, value in self.required_when.items()):
            raise ValueError('Invalid model condition')
        return self


def default_mapping(definition, fallback):
    binding = definition.get('model_binding')
    return dict(binding['environment']) if binding else dict(fallback)


def model_required(definition, config):
    binding = definition.get('model_binding')
    if not binding:
        return False
    properties = definition.get('config_schema', {}).get('properties', {})
    return all(config.get(key, properties.get(key, {}).get('default')) == value
               for key, value in binding['required_when'].items())


def validate_mapping(definition, mapping):
    binding = definition.get('model_binding')
    if binding and any(mapping.get(key) != value for key, value in binding['environment'].items()):
        raise KnowledgeError('model_mapping_invalid', '模型变量映射与插件声明不一致，请使用插件声明的变量名及对应连接字段', 409)


def validate_model_config(definition, config, model_id, mapping):
    if model_required(definition, config) and not model_id:
        raise KnowledgeError('model_required', '当前插件运行模式需要模型，请选择已授权的模型连接', 409)
    if model_id or model_required(definition, config):
        validate_mapping(definition, mapping)
