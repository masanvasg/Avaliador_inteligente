"""
Portal do Aluno — prova online com envio direto para o Google Sheets.

É um aplicativo Streamlit SEPARADO do app do professor (para o aluno não ter
acesso às ferramentas de geração/correção):

    streamlit run portal_aluno.py

Provas adaptativas: envie a cada grupo o link com o nível, por exemplo
    https://seu-app.streamlit.app/?nivel=Bom
Sem o parâmetro, o portal usa o nível "Regular".

Configuração (variáveis de ambiente ou Secrets — NUNCA no código):
    PLANILHA_RESPOSTAS_ID   ID da planilha que recebe as respostas
    SMTP_USUARIO/SMTP_SENHA e-mail e "Senha de app" para a cópia ao aluno (opcional)
    TURMAS, ESCOLA_PADRAO, NIVEL_ENSINO_PADRAO, DISCIPLINA_PADRAO, PORTAL_SEGURANCA
"""
from __future__ import annotations

import re
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage

import streamlit as st

from avaliador import (
    carregar_gabarito,
    garantir_cabecalho,
    montar_linha_resposta,
    processar_avaliacoes_personalizadas,
    selecionar_questoes,
)
from config import (
    DISCIPLINA_PADRAO,
    ESCOLA_PADRAO,
    ID_PLANILHA_RESPOSTAS,
    NIVEL_ENSINO_PADRAO,
    SEGURANCA_ATIVA,
    SMTP_PORTA,
    SMTP_SENHA,
    SMTP_SERVIDOR,
    SMTP_USUARIO,
    TURMAS,
    logger,
    obter_config,
)
from planilhas import conectar_sheets

st.set_page_config(page_title="Portal do Aluno - Avaliação", page_icon="📝", layout="centered")

NIVEIS_VALIDOS = ["Baixo", "Regular", "Bom", "Excelente"]
PADRAO_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ==============================================================================
# MOTOR DE SEGURANÇA (JavaScript)
# ==============================================================================
# Diferenças em relação à versão anterior:
#  - mostra uma TELA DE BLOQUEIO por cima, em vez de apagar a página inteira;
#  - o bloqueio fica registrado no navegador (sessionStorage): recarregar (F5)
#    não "limpa" a trava;
#  - evita registrar os mesmos ouvintes a cada atualização da tela;
#  - pode ser desligado após o envio (senão sair da aba depois de enviar bloquearia).
# Atenção: é uma barreira de DISSUASÃO no navegador do aluno, não uma proteção
# absoluta (quem desativa o JavaScript ou usa outro aparelho a contorna).
def _script_seguranca(ativar: bool) -> str:
    comando = "ativar" if ativar else "desativar"
    return f"""
<script>
(function () {{
  const w = window.parent, d = w.document, CHAVE = "avaliacao_bloqueada";

  if (!w.__avSeg) {{
    const bloquear = (motivo) => {{
      try {{ w.sessionStorage.setItem(CHAVE, motivo); }} catch (e) {{}}
      mostrar(motivo);
    }};
    const mostrar = (motivo) => {{
      if (d.getElementById("av-bloqueio")) return;
      const o = d.createElement("div");
      o.id = "av-bloqueio";
      o.style.cssText = "position:fixed;inset:0;z-index:2147483647;background:#fff;" +
        "display:flex;flex-direction:column;align-items:center;justify-content:center;" +
        "font-family:sans-serif;text-align:center;padding:24px;";
      o.innerHTML = "<h1 style='color:#c00'>🔒 AVALIAÇÃO BLOQUEADA</h1>" +
        "<p>Motivo: " + motivo + ".</p><p>Solicite um novo acesso ao professor.</p>";
      d.body.appendChild(o);
    }};
    const bloqueios = {{
      contextmenu: (e) => e.preventDefault(),
      copy: (e) => e.preventDefault(),
      cut: (e) => e.preventDefault(),
      paste: (e) => e.preventDefault(),
      visibilitychange: () => {{ if (d.hidden) bloquear("saída da página"); }},
    }};
    const aoPerderFoco = () => bloquear("perda de foco da janela");

    w.__avSeg = {{
      ativar() {{
        if (w.__avSegLigado) return;
        w.__avSegLigado = true;
        for (const [ev, fn] of Object.entries(bloqueios)) d.addEventListener(ev, fn);
        w.addEventListener("blur", aoPerderFoco);
        let motivo = null;
        try {{ motivo = w.sessionStorage.getItem(CHAVE); }} catch (e) {{}}
        if (motivo) mostrar(motivo);
      }},
      desativar() {{
        w.__avSegLigado = false;
        for (const [ev, fn] of Object.entries(bloqueios)) d.removeEventListener(ev, fn);
        w.removeEventListener("blur", aoPerderFoco);
        try {{ w.sessionStorage.removeItem(CHAVE); }} catch (e) {{}}
        const o = d.getElementById("av-bloqueio");
        if (o) o.remove();
      }},
    }};
  }}
  w.__avSeg.{comando}();
}})();
</script>
"""


def _injetar_html(html: str) -> None:
    """
    Executa um trecho de HTML/JS invisível na página.
    `st.components.v1.html` foi descontinuado (remoção anunciada para 2026-06-01);
    `st.iframe` o substitui. Versões antigas do Streamlit caem no método antigo.
    """
    if hasattr(st, "iframe"):
        st.iframe(html, height=1)
    else:
        import streamlit.components.v1 as components

        components.html(html, height=0, width=0)


# ==============================================================================
# UTILITÁRIOS
# ==============================================================================
def _agora() -> str:
    try:
        from zoneinfo import ZoneInfo

        momento = datetime.now(ZoneInfo(obter_config("FUSO_HORARIO", "America/Sao_Paulo")))
    except Exception:  # noqa: BLE001 — sem tzdata: usa o horário do servidor
        momento = datetime.now()
    return momento.strftime("%d/%m/%Y %H:%M:%S")


def _numero_da_linha_gravada(resposta_append: dict) -> int | None:
    """Extrai o número da linha do retorno do append_row ('Sheet1'!A5:L5 → 5)."""
    try:
        intervalo = resposta_append["updates"]["updatedRange"]
        return int(re.search(r"!\$?[A-Z]+\$?(\d+)", intervalo).group(1))
    except (KeyError, AttributeError, ValueError, TypeError):
        return None


def _enviar_copia_por_email(destino: str, nome: str, questoes: list[dict], respostas: list[str]) -> str | None:
    """Envia ao aluno a cópia das respostas. Devolve uma mensagem de aviso se falhar."""
    if not (SMTP_USUARIO and SMTP_SENHA):
        return None  # e-mail não configurado: simplesmente não envia

    nome_limpo = re.sub(r"[\r\n]+", " ", nome).strip()
    linhas = [f"Olá, {nome_limpo}!", "", "Aqui está a cópia das respostas que você enviou:", ""]
    for i, (q, resposta) in enumerate(zip(questoes, respostas), start=1):
        linhas += [f"Questão {i}: {q['pergunta']}", f"Sua resposta: {resposta or '(em branco)'}", ""]
    linhas.append("Bom descanso!\nSeu Professor.")

    mensagem = EmailMessage()
    mensagem["From"] = SMTP_USUARIO
    mensagem["To"] = destino
    mensagem["Subject"] = f"Cópia da sua Avaliação - {nome_limpo}"
    mensagem.set_content("\n".join(linhas))

    try:
        with smtplib.SMTP(SMTP_SERVIDOR, SMTP_PORTA, timeout=20) as servidor:
            servidor.starttls(context=ssl.create_default_context())
            servidor.login(SMTP_USUARIO, SMTP_SENHA)
            servidor.send_message(mensagem)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("Falha ao enviar cópia por e-mail: %s", e)
        return f"Suas respostas foram enviadas, mas não foi possível mandar a cópia por e-mail ({e})."


# ==============================================================================
# CARREGAMENTO DA PROVA
# ==============================================================================
st.session_state.setdefault("prova_enviada", False)
st.session_state.setdefault("enviando", False)
st.session_state.setdefault("erro_envio", None)
st.session_state.setdefault("aviso_envio", None)

if SEGURANCA_ATIVA:
    _injetar_html(_script_seguranca(ativar=not st.session_state["prova_enviada"]))

st.title("📝 Avaliação Oficial")

if st.session_state["prova_enviada"]:
    st.success("✅ Avaliação enviada com sucesso! Você já pode fechar esta aba.")
    if st.session_state["aviso_envio"]:
        st.warning(st.session_state["aviso_envio"])
    st.stop()

salvo = carregar_gabarito()
if not salvo:
    st.error("🛑 Nenhuma avaliação disponível no momento. O professor ainda não gerou a prova.")
    st.stop()

nivel_prova = ""
if salvo["tipo"] == "adaptativo" or isinstance(salvo["questoes"], dict):
    nivel_prova = str(st.query_params.get("nivel", "Regular")).strip().capitalize()
    if nivel_prova not in NIVEIS_VALIDOS:
        st.error("🛑 Link inválido: peça ao professor o link correto da sua avaliação.")
        st.stop()

try:
    questoes = selecionar_questoes(salvo["questoes"], nivel_prova or None)
except ValueError as e:
    st.error(f"🛑 A avaliação salva está inválida: {e}")
    st.stop()

disciplina = salvo["disciplina"] or DISCIPLINA_PADRAO

if SEGURANCA_ATIVA:
    st.warning(
        "⚠️ **Modo de Segurança Ativado:** não feche, não minimize e não mude de aba. "
        "A saída bloqueará a prova."
    )
st.write("---")

# ==============================================================================
# IDENTIFICAÇÃO
# ==============================================================================
st.subheader("Identificação")
st.text_input("Nome Completo:", key="id_nome")
st.text_input("Seu E-mail (para receber a cópia da prova):", key="id_email")
st.selectbox("Turma:", ["Selecione...", *TURMAS], key="id_turma")
st.write("---")

# ==============================================================================
# QUESTÕES
# ==============================================================================
st.subheader("Questões")
respostas: list[str] = []

for numero, questao in enumerate(questoes, start=1):
    enunciado = f"**{numero}. {questao['pergunta']}**"
    if questao["tipo"] == "objetiva":
        opcoes = [f"{letra}) {questao[letra]}" for letra in "ABCDE" if questao.get(letra)]
        resposta = st.radio(enunciado, opcoes, index=None, key=f"q_{numero}")
    else:
        resposta = st.text_area(enunciado, key=f"q_{numero}", height=150)
    respostas.append((resposta or "").strip())
    st.write("")

st.write("---")


# ==============================================================================
# ENVIO
# ==============================================================================
def _iniciar_envio() -> None:
    """Valida a identificação ANTES de travar o botão (evita envio duplo por duplo clique)."""
    nome = st.session_state.get("id_nome", "").strip()
    email = st.session_state.get("id_email", "").strip()
    turma = st.session_state.get("id_turma", "Selecione...")

    if not nome or turma == "Selecione...":
        st.session_state["erro_envio"] = "⚠️ Preencha seu nome e sua turma antes de enviar."
    elif email and not PADRAO_EMAIL.match(email):
        st.session_state["erro_envio"] = "⚠️ O e-mail informado não parece válido. Corrija ou deixe em branco."
    else:
        st.session_state["erro_envio"] = None
        st.session_state["enviando"] = True


em_branco = [str(i) for i, r in enumerate(respostas, start=1) if not r]
if em_branco:
    st.info(f"Questões ainda sem resposta: {', '.join(em_branco)}.")

if st.session_state["erro_envio"]:
    st.error(st.session_state["erro_envio"])

st.button(
    "Finalizar e Enviar Prova",
    type="primary",
    on_click=_iniciar_envio,
    disabled=st.session_state["enviando"],
)

if st.session_state["enviando"]:
    nome = st.session_state["id_nome"].strip()
    email = st.session_state["id_email"].strip()
    turma = st.session_state["id_turma"]

    with st.spinner("Enviando suas respostas de forma segura..."):
        # 1) Gravar na planilha — se isto falhar, o aluno precisa saber que NÃO enviou.
        try:
            folha = conectar_sheets(ID_PLANILHA_RESPOSTAS)
            cabecalho = garantir_cabecalho(folha, folha.row_values(1), len(questoes))
            linha = montar_linha_resposta(
                cabecalho,
                {
                    "data_hora": _agora(),
                    "nome": nome,
                    "email": email,
                    "turma": turma,
                    "escola": ESCOLA_PADRAO,
                    "nivel_ensino": NIVEL_ENSINO_PADRAO,
                    "disciplina": disciplina,
                    "nivel_prova": nivel_prova,
                },
                respostas,
            )
            numero_linha = _numero_da_linha_gravada(folha.append_row(linha))  # RAW: não executa fórmulas
        except Exception as e:  # noqa: BLE001
            logger.exception("Falha ao gravar a prova no Sheets")
            st.session_state["enviando"] = False
            st.session_state["erro_envio"] = (
                f"❌ Suas respostas NÃO foram enviadas. Tente novamente em instantes "
                f"ou avise o professor. (Detalhe: {e})"
            )
            st.rerun()

        # 2) Correção imediata SÓ DESTA linha — falha aqui não pode perder a prova.
        if numero_linha:
            try:
                processar_avaliacoes_personalizadas(
                    folha,
                    salvo["questoes"],
                    salvo["tipo"],
                    nivel=nivel_prova or None,
                    linhas={numero_linha},
                )
            except Exception:  # noqa: BLE001
                logger.exception("Correção automática falhou; o professor pode reprocessar no app.")

        # 3) Cópia por e-mail (opcional) — independente do passo anterior.
        st.session_state["aviso_envio"] = (
            _enviar_copia_por_email(email, nome, questoes, respostas) if email else None
        )

    st.session_state["prova_enviada"] = True
    st.session_state["enviando"] = False
    st.rerun()
