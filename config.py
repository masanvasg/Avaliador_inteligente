"""
Configuração central do Sistema de Avaliação Inteligente.

Tudo que varia entre máquinas/contas (IDs de pasta, chaves, modelo, senhas)
fica aqui, lido de variáveis de ambiente ou dos Secrets do Streamlit — nunca
chumbado no meio da interface.

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


def _lista(chave: str, padrao: list[str]) -> list[str]:
    """Lê uma lista separada por vírgulas (ex.: TURMAS="6º A, 7º B")."""
    bruto = obter_config(chave)
    if not bruto:
        return padrao
    itens = [item.strip() for item in bruto.split(",") if item.strip()]
    return itens or padrao


# --------------------------------------------------------------------------
# Modelo de IA
# --------------------------------------------------------------------------
NOME_MODELO_GEMINI = obter_config("GEMINI_MODEL", "gemini-3.6-flash")
_FALLBACK_PADRAO = obter_config("GEMINI_MODEL_FALLBACK", "gemini-2.5-flash")

# Lista ordenada e SEM repetições: o primeiro é o preferido; os demais só
# entram em ação se o anterior ficar indisponível.
MODELOS_FALLBACK: list[str] = list(
    dict.fromkeys(m for m in (NOME_MODELO_GEMINI, _FALLBACK_PADRAO) if m)
)


# --------------------------------------------------------------------------
# Google Drive / Sheets
# --------------------------------------------------------------------------
ID_PASTA_PROVAS = obter_config("DRIVE_PASTA_PROVAS", "")
ID_PASTA_REDACOES = obter_config("DRIVE_PASTA_REDACOES", "")

# Planilha que recebe as respostas do Portal do Aluno.
ID_PLANILHA_RESPOSTAS = obter_config("PLANILHA_RESPOSTAS_ID", "")
CAMINHO_CREDENCIAIS_SERVICO = obter_config("GOOGLE_CREDENTIALS_PATH", "credenciais.json")


# --------------------------------------------------------------------------
# Portal do Aluno
# --------------------------------------------------------------------------
TURMAS = _lista("TURMAS", ["6º Ano", "7º Ano", "8º Ano", "9º Ano"])
ESCOLA_PADRAO = obter_config("ESCOLA_PADRAO", ["Escola Estadual do Campo São João", "Colégio Estadual Quinino Bocaiúva"])
NIVEL_ENSINO_PADRAO = obter_config("NIVEL_ENSINO_PADRAO", ["Ensino Fundamental", "Ensino Médio", "Ensino Técnico Profissionalizante", "EJA"])
DISCIPLINA_PADRAO = obter_config("DISCIPLINA_PADRAO", ["Ciências", "Educação Financeira", "Gestão em Saúde Ocupacional", "Higiene Ocupacional"])
SEGURANCA_ATIVA = (obter_config("PORTAL_SEGURANCA", "1") or "1").lower() not in ("0", "false", "nao", "não")

# E-mail de confirmação ao aluno (opcional). A senha NUNCA deve ficar no código:
# use uma "Senha de app" do Google guardada em variável de ambiente ou Secrets.
SMTP_SERVIDOR = obter_config("SMTP_SERVIDOR", "smtp.gmail.com")
SMTP_PORTA = int(obter_config("SMTP_PORTA", "587") or 587)
SMTP_USUARIO = obter_config("SMTP_USUARIO", "")
SMTP_SENHA = obter_config("SMTP_SENHA", "")


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
PONTOS_POR_OBJETIVA = 1.0
PONTOS_POR_DISCURSIVA = 2.0

# Faixas de conceito em PERCENTUAL da nota máxima possível (0–100).
# Cada faixa vale para percentual ESTRITAMENTE MENOR que o limite
# (a original deixava lacunas: 59,5% caía em "Bom").
#   < 40%  → Baixo      40–59,99% → Regular
#   60–79,99% → Bom     ≥ 80%  → Excelente
FAIXAS_CONCEITO = [
    (40.0, "Baixo"),
    (60.0, "Regular"),
    (80.0, "Bom"),
    (float("inf"), "Excelente"),
]

NIVEIS_ADAPTATIVOS = ["Baixo", "Regular", "Bom", "Excelente"]
