import os
import json
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/drive",
]

ID_DA_PASTA = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "1d5S_NOKGI0mxJHkwnxgZfoR27EZxCS4e")


def _validar_json_caminho(caminho, nome_amigavel):
    """Verifica se o arquivo JSON existe, não está vazio e é válido."""
    if not os.path.exists(caminho):
        raise FileNotFoundError(
            f"Arquivo '{caminho}' não encontrado. "
            f"Coloque o {nome_amigavel} baixado do Google Cloud na pasta do projeto."
        )

    with open(caminho, 'r', encoding='utf-8') as f:
        conteudo = f.read().strip()

    if not conteudo:
        raise ValueError(
            f"O arquivo '{caminho}' está VAZIO (0 bytes). "
            f"Baixe o {nome_amigavel} correto do Google Cloud Console e substitua."
        )

    try:
        dados = json.loads(conteudo)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"O arquivo '{caminho}' não é um JSON válido. "
            f"Erro: {e}. Verifique se copiou o conteúdo completo."
        )

    return dados


def autenticar_usuario():
    creds = None

    # 1. Tenta Streamlit secrets (Cloud)
    try:
        import streamlit as st
        if "google_token" in st.secrets:
            info = dict(st.secrets["google_token"])
            creds = Credentials.from_authorized_user_info(info, SCOPES)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                return creds
            if creds and creds.valid:
                return creds
    except Exception:
        pass

    # 2. Tenta token.json (local, gerado após primeira autorização)
    if os.path.exists('token.json'):
        try:
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                return creds
            if creds and creds.valid:
                return creds
        except Exception:
            pass  # token.json pode estar corrompido, segue para recriar

    # 3. Fluxo local — precisa do cliente_oauth.json
    if not os.path.exists('cliente_oauth.json'):
        raise FileNotFoundError(
            "Arquivo 'cliente_oauth.json' não encontrado.\n"
            "1. Vá em https://console.cloud.google.com/apis/credentials\n"
            "2. Clique em '+ CREATE CREDENTIALS' → 'OAuth client ID'\n"
            "3. Tipo: Desktop app | Nome: Avaliador Forms\n"
            "4. Baixe o JSON, renomeie para 'cliente_oauth.json' e cole na pasta."
        )

    _validar_json_caminho('cliente_oauth.json', 'OAuth Client ID')

    flow = InstalledAppFlow.from_client_secrets_file('cliente_oauth.json', SCOPES)
    creds = flow.run_local_server(port=0)

    with open('token.json', 'w') as token:
        token.write(creds.to_json())

    return creds


def _montar_requests_forms(questoes):
    """Monta a lista de requests do batchUpdate: cabeçalho fixo + questões."""
    requests = []
    cabecalho = [
        "1. Nome Completo:",
        "2. Turma:",
        "3. Escola:",
        "4. Nível de Ensino:",
        "5. Componente Curricular:",
    ]

    for i, pergunta in enumerate(cabecalho):
        requests.append({
            "createItem": {
                "item": {
                    "title": pergunta,
                    "questionItem": {
                        "question": {
                            "required": True,
                            "textQuestion": {"paragraph": False}
                        }
                    }
                },
                "location": {"index": i}
            }
        })

    for i, q in enumerate(questoes):
        index_atual = i + len(cabecalho)
        titulo = f"Questão {i + 1}: {q.get('pergunta', '')}"

        if q.get("tipo") == "discursiva":
            item = {
                "createItem": {
                    "item": {
                        "title": titulo,
                        "questionItem": {
                            "question": {
                                "required": True,
                                "textQuestion": {"paragraph": True}
                            }
                        }
                    },
                    "location": {"index": index_atual}
                }
            }
        else:
            opcoes = [
                {"value": f"{letra}) {q[letra]}"}
                for letra in ["A", "B", "C", "D", "E"]
                if q.get(letra)
            ]
            item = {
                "createItem": {
                    "item": {
                        "title": titulo,
                        "questionItem": {
                            "question": {
                                "required": True,
                                "choiceQuestion": {
                                    "type": "RADIO",
                                    "options": opcoes
                                }
                            }
                        }
                    },
                    "location": {"index": index_atual}
                }
            }
        requests.append(item)

    return requests


def criar_formulario_ia(questoes_json, disciplina):
    creds = autenticar_usuario()
    forms_service = build('forms', 'v1', credentials=creds)
    drive_service = build('drive', 'v3', credentials=creds)

    questoes = json.loads(questoes_json) if isinstance(questoes_json, str) else questoes_json

    form_body = {
        "info": {
            "title": f"Avaliação Inteligente - {disciplina}",
            "documentTitle": f"Avaliação - {disciplina}"
        }
    }
    form_criado = forms_service.forms().create(body=form_body).execute()
    form_id = form_criado["formId"]

    requests = _montar_requests_forms(questoes)
    forms_service.forms().batchUpdate(formId=form_id, body={"requests": requests}).execute()

    try:
        form_file = drive_service.files().get(fileId=form_id, fields='parents').execute()
        previous_parents = ",".join(form_file.get('parents', []))
        drive_service.files().update(
            fileId=form_id,
            addParents=ID_DA_PASTA,
            removeParents=previous_parents,
            fields='id, parents'
        ).execute()
    except Exception as e:
        print(f"⚠️ Não foi possível mover o formulário para a pasta do Drive: {e}")

    return f"https://docs.google.com/forms/d/{form_id}/edit"
