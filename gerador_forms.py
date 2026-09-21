"""
Integração com Google Forms, Drive e Docs.

  - autenticação OAuth (local ou via Secrets no Streamlit Cloud);
  - criação do formulário com cabeçalho de identificação + questões;
  - movimentação do arquivo para a pasta da turma;
  - geração do relatório de diagnóstico em Google Docs.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import CAMINHO_CLIENTE_OAUTH, CAMINHO_TOKEN, PASTA_BASE, logger

SCOPES = [
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]

CAMPOS_IDENTIFICACAO = [
    "1. Nome Completo:",
    "2. E-mail",
    "3. Turma:",
    "4. Escola:",
    "5. Nível de Ensino:",
    "6. Componente Curricular:",
]

LETRAS_ALTERNATIVAS = ["A", "B", "C", "D", "E"]


# ---------------------------------------------------------------------------
# Utilitários de link
# ---------------------------------------------------------------------------
def extrair_id_pasta(texto: str) -> str:
    """Aceita o link completo da pasta do Drive ou o ID cru."""
    if not texto:
        return ""
    texto = texto.strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", texto)
    return m.group(1) if m else texto


def extrair_id_planilha(texto: str) -> str:
    """Aceita o link completo da planilha ou o ID cru."""
    if not texto:
        return ""
    texto = texto.strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", texto)
    return m.group(1) if m else texto


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------
def _validar_json_caminho(caminho: str, nome_amigavel: str) -> dict:
    """Confere se o arquivo de credenciais existe, tem conteúdo e é JSON válido."""
    if not os.path.exists(caminho):
        raise FileNotFoundError(
            f"Arquivo '{caminho}' não encontrado. Coloque o {nome_amigavel} "
            "baixado do Google Cloud na pasta do projeto."
        )

    with open(caminho, "r", encoding="utf-8") as f:
        conteudo = f.read().strip()

    if not conteudo:
        raise ValueError(
            f"O arquivo '{caminho}' está vazio. Baixe novamente o {nome_amigavel} "
            "no Google Cloud Console e substitua."
        )

    try:
        return json.loads(conteudo)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"O arquivo '{caminho}' não é um JSON válido ({e}). "
            "Verifique se o conteúdo foi copiado por inteiro."
        ) from e


def _credenciais_dos_secrets() -> Credentials | None:
    try:
        import streamlit as st

        if "google_token" not in st.secrets:
            return None
        info = dict(st.secrets["google_token"])
    except Exception:
        return None

    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds if creds.valid else None


def _credenciais_do_token() -> Credentials | None:
    if not os.path.exists(CAMINHO_TOKEN):
        return None
    try:
        creds = Credentials.from_authorized_user_file(CAMINHO_TOKEN, SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds if creds.valid else None
    except Exception:
        logger.warning("token.json inválido ou expirado — será refeito o login.")
        return None


def autenticar_usuario() -> Credentials:
    """
    Obtém credenciais OAuth na ordem: Secrets → token.json → navegador (local).

    O terceiro caminho abre um servidor local e por isso só funciona na máquina
    do professor. Na nuvem, a mensagem de erro explica exatamente o que fazer.
    """
    for obter in (_credenciais_dos_secrets, _credenciais_do_token):
        creds = obter()
        if creds:
            return creds

    if os.environ.get("STREAMLIT_RUNTIME") or os.environ.get("STREAMLIT_SERVER_HEADLESS"):
        raise RuntimeError(
            "Não há credenciais OAuth válidas neste ambiente. Gere o token.json "
            "na sua máquina e cole o conteúdo dele no bloco [google_token] em "
            "Settings > Secrets do app."
        )

    if not os.path.exists(CAMINHO_CLIENTE_OAUTH):
        raise FileNotFoundError(
            f"Arquivo 'cliente_oauth.json' não encontrado em '{PASTA_BASE}'.\n"
            "1. Acesse https://console.cloud.google.com/apis/credentials\n"
            "2. '+ CREATE CREDENTIALS' → 'OAuth client ID'\n"
            "3. Tipo: Desktop app | Nome: Avaliador Forms\n"
            "4. Baixe o JSON, renomeie para 'cliente_oauth.json' e salve na pasta do projeto."
        )

    _validar_json_caminho(CAMINHO_CLIENTE_OAUTH, "OAuth Client ID")

    flow = InstalledAppFlow.from_client_secrets_file(CAMINHO_CLIENTE_OAUTH, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(CAMINHO_TOKEN, "w", encoding="utf-8") as token:
        token.write(creds.to_json())

    return creds


def _servicos(creds: Credentials) -> dict:
    return {
        "forms": build("forms", "v1", credentials=creds, cache_discovery=False),
        "drive": build("drive", "v3", credentials=creds, cache_discovery=False),
        "docs": build("docs", "v1", credentials=creds, cache_discovery=False),
    }


# ---------------------------------------------------------------------------
# Montagem do formulário
# ---------------------------------------------------------------------------
def _item_texto(titulo: str, index: int, paragrafo: bool = False) -> dict:
    return {
        "createItem": {
            "item": {
                "title": titulo,
                "questionItem": {
                    "question": {
                        "required": True,
                        "textQuestion": {"paragraph": paragrafo},
                    }
                },
            },
            "location": {"index": index},
        }
    }


def _item_multipla_escolha(titulo: str, index: int, questao: dict) -> dict:
    opcoes = [
        {"value": f"{letra}) {questao[letra]}"}
        for letra in LETRAS_ALTERNATIVAS
        if questao.get(letra)
    ]
    return {
        "createItem": {
            "item": {
                "title": titulo,
                "questionItem": {
                    "question": {
                        "required": True,
                        "choiceQuestion": {"type": "RADIO", "options": opcoes},
                    }
                },
            },
            "location": {"index": index},
        }
    }


def montar_requests_forms(questoes: list[dict]) -> list[dict]:
    """Cabeçalho fixo de identificação + uma questão por item."""
    requests: list[dict] = [
        _item_texto(campo, i) for i, campo in enumerate(CAMPOS_IDENTIFICACAO)
    ]

    for i, q in enumerate(questoes):
        index = i + len(CAMPOS_IDENTIFICACAO)
        titulo = f"Questão {i + 1}: {q.get('pergunta', '').strip()}"

        if q.get("tipo") == "discursiva":
            requests.append(_item_texto(titulo, index, paragrafo=True))
        else:
            requests.append(_item_multipla_escolha(titulo, index, q))

    return requests


def _mover_para_pasta(drive_service, file_id: str, id_pasta_destino: str) -> None:
    """Move o arquivo recém-criado para a pasta da turma, se houver uma."""
    if not id_pasta_destino:
        return
    try:
        atual = drive_service.files().get(fileId=file_id, fields="parents").execute()
        pais_antigos = ",".join(atual.get("parents", []))
        drive_service.files().update(
            fileId=file_id,
            addParents=id_pasta_destino,
            removeParents=pais_antigos,
            fields="id, parents",
        ).execute()
    except Exception as e:  # noqa: BLE001 — o formulário já existe; mover é secundário
        logger.warning("Não foi possível mover %s para a pasta do Drive: %s", file_id, e)


def criar_formulario_ia(
    questoes_json: str | list[dict],
    disciplina: str,
    id_pasta_destino: str = "",
) -> str:
    """
    Cria o Google Forms da avaliação e devolve o link de edição.

    `questoes_json` aceita tanto a string JSON quanto a lista já desserializada.
    `id_pasta_destino` é opcional: sem ele, o formulário fica na raiz do Drive.
    """
    questoes: Any = (
        json.loads(questoes_json) if isinstance(questoes_json, str) else questoes_json
    )

    if not isinstance(questoes, list) or not questoes:
        raise ValueError("A lista de questões está vazia ou em formato inesperado.")

    creds = autenticar_usuario()
    servicos = _servicos(creds)

    form = servicos["forms"].forms().create(
        body={
            "info": {
                "title": f"Avaliação Inteligente - {disciplina}",
                "documentTitle": f"Avaliação - {disciplina}",
            }
        }
    ).execute()

    form_id = form["formId"]

    servicos["forms"].forms().batchUpdate(
        formId=form_id, body={"requests": montar_requests_forms(questoes)}
    ).execute()

    _mover_para_pasta(servicos["drive"], form_id, id_pasta_destino)

    logger.info("Formulário criado: %s (%d questões)", form_id, len(questoes))
    return f"https://docs.google.com/forms/d/{form_id}/edit"


# ---------------------------------------------------------------------------
# Relatório em Google Docs
# ---------------------------------------------------------------------------
def criar_relatorio_google_docs(
    nome_aluno: str,
    texto_diagnostico: str,
    id_pasta_destino: str = "",
) -> str:
    """Cria um Google Docs com o diagnóstico do aluno e devolve o link."""
    if not texto_diagnostico.strip():
        raise ValueError("O diagnóstico está vazio — nada para salvar.")

    creds = autenticar_usuario()
    servicos = _servicos(creds)

    metadados: dict[str, Any] = {
        "name": f"Diagnóstico DUA - {nome_aluno}",
        "mimeType": "application/vnd.google-apps.document",
    }
    if id_pasta_destino:
        metadados["parents"] = [id_pasta_destino]

    arquivo = servicos["drive"].files().create(body=metadados, fields="id").execute()
    id_documento = arquivo["id"]

    servicos["docs"].documents().batchUpdate(
        documentId=id_documento,
        body={
            "requests": [
                {"insertText": {"location": {"index": 1}, "text": texto_diagnostico}}
            ]
        },
    ).execute()

    logger.info("Relatório criado para %s: %s", nome_aluno, id_documento)
    return f"https://docs.google.com/document/d/{id_documento}/edit"
