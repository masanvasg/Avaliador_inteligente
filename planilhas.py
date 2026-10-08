"""
Acesso ao Google Sheets compartilhado pelo app do professor e pelo Portal do Aluno.

Autenticação por conta de serviço: bloco [gcp_service_account] nos Secrets do
Streamlit ou arquivo JSON local (GOOGLE_CREDENTIALS_PATH).
"""
from __future__ import annotations

import os

import gspread
import streamlit as st
from google.oauth2.service_account import Credentials as ServiceAccountCredentials

from config import CAMINHO_CREDENCIAIS_SERVICO

ESCOPO_SHEETS = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


@st.cache_resource(show_spinner=False)
def cliente_sheets() -> gspread.Client:
    """Cliente gspread autenticado (reaproveitado entre execuções do Streamlit)."""
    try:
        usar_secrets = "gcp_service_account" in st.secrets
    except Exception:  # sem secrets.toml
        usar_secrets = False

    if usar_secrets:
        credenciais = ServiceAccountCredentials.from_service_account_info(
            dict(st.secrets["gcp_service_account"]), scopes=ESCOPO_SHEETS
        )
    else:
        if not os.path.exists(CAMINHO_CREDENCIAIS_SERVICO):
            raise FileNotFoundError(
                f"Credenciais da conta de serviço não encontradas em "
                f"'{CAMINHO_CREDENCIAIS_SERVICO}'. Configure GOOGLE_CREDENTIALS_PATH "
                "ou o bloco [gcp_service_account] nos Secrets."
            )
        credenciais = ServiceAccountCredentials.from_service_account_file(
            CAMINHO_CREDENCIAIS_SERVICO, scopes=ESCOPO_SHEETS
        )
    return gspread.authorize(credenciais)


def conectar_sheets(id_planilha: str) -> gspread.Worksheet:
    """Abre a primeira aba da planilha, com mensagens de erro compreensíveis."""
    if not id_planilha:
        raise ValueError("ID da planilha não informado.")
    try:
        return cliente_sheets().open_by_key(id_planilha).sheet1
    except gspread.exceptions.SpreadsheetNotFound as e:
        raise RuntimeError(
            "Planilha não encontrada. Confira o link e se ela foi compartilhada "
            "com o e-mail da conta de serviço (permissão de Editor)."
        ) from e
    except gspread.exceptions.APIError as e:
        raise RuntimeError(
            "Não foi possível abrir a planilha. Confira o link e verifique se ela "
            "foi compartilhada com o e-mail da conta de serviço (permissão de Editor)."
        ) from e
