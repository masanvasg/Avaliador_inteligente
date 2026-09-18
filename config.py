"""
Configuração central do Sistema de Avaliação Inteligente.
Tudo que varia entre máquinas/contas (IDs de pasta, chaves, modelo) fica aqui,
lido de variáveis de ambiente ou dos Secrets do Streamlit — nunca chumbado no
meio da interface.
Ordem de prioridade: variável de ambiente > st.secrets > valor padrão.
"""
from __future__ import annotations

import logging
import os

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("avaliacao")

def obter_config(chave: str, padrao: str | None = None) -> str | None:
    """Busca uma configuração no ambiente e, se não achar, nos Secrets do Streamlit."""
    valor = os.environ.get(chave)
    if valor:
        return valor.strip()
    try:
        import streamlit as st
        if chave in st.secrets:
            return str(st.secrets[chave]).strip()
    except Exception:  # streamlit ausente ou secrets.toml não configurado
        pass
    return padrao

# --------------------------------------------------------------------------
# Modelo de IA
# --------------------------------------------------------------------------
NOME_MODELO_GEMINI = obter_config("GEMINI_MODEL", "gemini-3.6-flash")
MODELOS_FALLBACK = [
    NOME_MODELO_GEMINI,
    "gemini-3.6-flash",
]

# --------------------------------------------------------------------------
# Google Drive — pastas de destino
# --------------------------------------------------------------------------
ID_PASTA_PROVAS = obter_config("DRIVE_PASTA_PROVAS", "")
ID_PASTA_REDACOES = obter_config("DRIVE_PASTA_REDACOES", "")

# --------------------------------------------------------------------------
# Arquivos de trabalho
# --------------------------------------------------------------------------
PASTA_BASE = os.path.dirname(os.path.abspath(__file__))
ARQUIVO_GABARITO = os.path.join(PASTA_BASE, "ultimo_gabarito.json")
CAMINHO_CLIENTE_OAUTH = os.path.join(PASTA_BASE, "cliente_oauth.json")
CAMINHO_TOKEN = os.path.join(PASTA_BASE, "token.json")

# --------------------------------------------------------------------------
# Regras pedagógicas
# --------------------------------------------------------------------------
PONTOS_POR_DISCURSIVA = 2.0

# Faixas de conceito em PERCENTUAL da nota máxima possível (0–100).
FAIXAS_CONCEITO = [
    (40.0, "Baixo"),
    (59.0, "Regular"),
    (80.0, "Bom"),
    (100.0, "Excelente"),
]

NIVEIS_ADAPTATIVOS = ["Baixo", "Regular", "Bom", "Excelente"]
