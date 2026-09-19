"""
Sistema de Avaliação Inteligente — interface Streamlit.
Fluxo:
  Aba 1 — material didático, folha de redação e geração da prova no Google Forms
  Aba 2 — correção multimodal de redações manuscritas (foto → transcrição → DUA)
  Aba 3 — processamento das notas na planilha e painel da turma
"""
from __future__ import annotations

import io
import json
import os
import re
import traceback

import gspread
import pandas as pd
import streamlit as st
from fpdf import FPDF
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from PIL import Image
from pypdf import PdfReader
from pptx import Presentation  # <--- Import necessário para ler slides

from avaliador import (
    NOME_MODELO_GEMINI,
    carregar_json_ia,
    configurar_gemini,
    gerar_com_retry,
    processar_avaliacoes_personalizadas,
)
from config import (
    ARQUIVO_GABARITO,
    ID_PASTA_PROVAS,
    ID_PASTA_REDACOES,
    NIVEIS_ADAPTATIVOS,
    logger,
    obter_config,
)
from gerador_forms import (
    criar_formulario_ia,
    criar_relatorio_google_docs,
    extrair_id_pasta,
    extrair_id_planilha,
)

# set_page_config precisa ser a PRIMEIRA chamada Streamlit do script
st.set_page_config(
    page_title="Sistema de Avaliação Inteligente",
    page_icon="🎓",
    layout="wide",
)

DISCIPLINAS = [
    "Ciências", "Biologia", "Física", "Química", "Geografia",
    "História", "Sociologia", "Filosofia", "Inglês", "Espanhol",
    "Ed. Financeira", "Ed. Digital", "Ed. Ambiental", "Sustentabilidade",
    "Matemática", "Português", "Robótica", "Programação", "Ed. Física", "Artes",
]

GENEROS_TEXTUAIS = [
    "Dissertação-Argumentativa", "Relatório Técnico", "Artigo de Opinião",
    "Crônica", "Carta Aberta", "Texto Livre",
]

ESCOPO_SHEETS = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# ===========================================================================
# PERSISTÊNCIA DO GABARITO
# ===========================================================================
def salvar_gabarito_em_disco(gabarito, tipo_gabarito: str) -> None:
    try:
        with open(ARQUIVO_GABARITO, "w", encoding="utf-8") as f:
            json.dump(
                {"tipo": tipo_gabarito, "questoes": gabarito},
                f, ensure_ascii=False, indent=2,
            )
    except OSError as e:
        logger.warning("Não foi possível salvar o gabarito em disco: %s", e)

def carregar_gabarito_salvo() -> None:
    if "gabarito" in st.session_state or not os.path.exists(ARQUIVO_GABARITO):
        return
    try:
        with open(ARQUIVO_GABARITO, "r", encoding="utf-8") as f:
            conteudo = f.read().strip()
        if not conteudo:
            return
        dados = json.loads(conteudo)
        if isinstance(dados, dict) and "questoes" in dados:
            questoes = dados["questoes"]
            tipo = dados.get("tipo", "diagnostico")
        else:
            questoes = dados
            tipo = (
                "adaptativo"
                if isinstance(dados, dict) and any(n in dados for n in NIVEIS_ADAPTATIVOS)
                else "diagnostico"
            )
        st.session_state["gabarito"] = questoes
        st.session_state["tipo_gabarito"] = tipo
        st.session_state["gabarito_restaurado"] = True
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Gabarito salvo ilegível (%s) — ignorando.", e)

# ===========================================================================
# FOLHA DE REDAÇÃO EM PDF
# ===========================================================================
class FolhaProducao(FPDF):
    def header(self) -> None:
        self.set_font("Helvetica", "B", 15)
        self.cell(0, 10, "Folha Oficial de Produção Textual", align="C")
        self.ln(12)

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(130, 130, 130)
        self.cell(0, 10, f"Página {self.page_no()}", align="C")

@st.cache_data(show_spinner=False)
def criar_pdf_redacao(
    disciplina: str,
    tema: str,
    genero: str,
    linhas: int = 20,
) -> bytes:
    pdf = FolhaProducao()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Nome: " + "_" * 72)
    pdf.ln(8)
    pdf.cell(
        0, 8,
        f"Componente: {disciplina}        Turma: ____________        Data: ____/____/20___",
    )
    pdf.ln(12)

    pdf.set_fill_color(245, 245, 245)
    pdf.set_font("Helvetica", "B", 11)
    tema_final = (tema or "").strip() or "Tema Livre"
    pdf.multi_cell(
        0, 8,
        f"Tema proposto: {tema_final}\nGênero textual: {genero}",
        border=1, fill=True, align="L",
    )
    pdf.ln(8)

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(130, 130, 130)
    for i in range(1, linhas + 1):
        y = pdf.get_y()
        pdf.cell(8, 9, str(i), align="R")
        pdf.line(22, y + 6.5, 200, y + 6.5)
        pdf.ln(9.5)

    saida = pdf.output()
    return bytes(saida)

# ===========================================================================
# LEITURA DO MATERIAL DIDÁTICO
# ===========================================================================
@st.cache_data(show_spinner=False)
def _extrair_texto_pdf(conteudo: bytes) -> str:
    leitor = PdfReader(io.BytesIO(conteudo))
    return "\n".join((pagina.extract_text() or "") for pagina in leitor.pages)

def preparar_conteudo_para_ia(arquivos) -> tuple[list, str]:
    pacote: list = []
    texto_total = ""
    avisos: list[str] = []
    for arquivo in arquivos or []:
        try:
            nome_minusculo = arquivo.name.lower()
            
            if nome_minusculo.endswith(".pdf"):
                texto = _extrair_texto_pdf(arquivo.getvalue())
                if texto.strip():
                    texto_total += texto + "\n"
                else:
                    avisos.append(
                        f"'{arquivo.name}' parece ser um PDF digitalizado sem texto. "
                        "Envie como imagem para a IA conseguir ler."
                    )
            
            elif nome_minusculo.endswith(".pptx"):
                # Leitura de arquivos PPTX (Textos e Imagens embutidas)
                apresentacao = Presentation(arquivo)
                for slide in apresentacao.slides:
                    for shape in slide.shapes:
                        # 1. Tenta ler os textos nas caixas nativas (se existirem)
                        if shape.has_text_frame:
                            for paragraph in shape.text_frame.paragraphs:
                                texto_extraido_total += paragraph.text + "\n"
                        
                        # 2. NOVA REGRA: Extrai as imagens fixadas no slide
                        if hasattr(shape, "image"):
                            bytes_imagem = shape.image.blob
                            imagem_extraida = Image.open(io.BytesIO(bytes_imagem))
                            conteudo_para_ia.append(imagem_extraida)
                            
                st.success(f"✅ Apresentação PPTX '{arquivo.name}' lida com sucesso (textos e imagens extraídos)!")
            elif nome_minusculo.endswith((".png", ".jpg", ".jpeg")):
                pacote.append(Image.open(arquivo))
                
            else:
                avisos.append(f"O formato do arquivo '{arquivo.name}' não é suportado.")
                
        except Exception as e:
            avisos.append(f"Não foi possível ler '{arquivo.name}': {e}")
            
    if texto_total.strip():
        pacote.append(texto_total)
    return pacote, " ".join(avisos)

# ===========================================================================
# PROMPTS
# ===========================================================================
def montar_prompt_prova(
    disciplina: str,
    total_questoes: int,
    objetivas: int,
    discursivas: int,
    adaptativa: bool,
) -> str:
    regra_interdisciplinar = ""
    if disciplina not in ("Português", "Matemática"):
        regra_interdisciplinar = (
            "REGRA DE INTERDISCIPLINARIDADE: sempre que o conteúdo permitir, "
            "integre raciocínio lógico-matemático ou interpretação de texto aprofundada."
        )

    if adaptativa:
        instrucao_saida = f"""
Crie 4 provas diferentes a partir deste material, cada uma com exatamente {total_questoes} questões,
ajustando o nível cognitivo: 'Baixo' (direta e básica), 'Regular' (intermediária),
'Bom' (análise e relação) e 'Excelente' (alta complexidade e síntese).

Devolva um único objeto JSON no formato de dicionário com as 4 listas:
{{
  "Baixo": [{{"tipo": "objetiva", "pergunta": "...", "A": "...", "B": "...", "C": "...", "D": "...", "E": "...", "correta": "C"}}],
  "Regular": [...],
  "Bom": [...],
  "Excelente": [...]
}}
"""
    else:
        instrucao_saida = f"""
Crie uma avaliação única e equilibrada para toda a turma, com exatamente {total_questoes} questões.

Devolva EXATAMENTE uma lista JSON:
[
  {{"tipo": "objetiva", "pergunta": "...", "A": "...", "B": "...", "C": "...", "D": "...", "E": "...", "correta": "C"}},
  {{"tipo": "discursiva", "pergunta": "...", "criterio_correcao": "..."}}
]
"""

    return f"""
Atue como um professor especialista em {disciplina}.
Leia o material de apoio fornecido e construa a avaliação.
{regra_interdisciplinar}

REGRAS DE ESTRUTURAÇÃO (para cada prova gerada):
1. {objetivas} questões objetivas, múltipla escolha de A a E, com o campo "correta" indicando a letra.
2. {discursivas} questões discursivas, com "pergunta" e "criterio_correcao" (valendo até 2,0 pontos).
3. Não use aspas duplas dentro dos textos; se precisar citar, use aspas simples.
4. Não use quebras de linha literais dentro dos valores JSON.
5. Devolva apenas o JSON, sem comentários e sem blocos de código.

{instrucao_saida}
""".strip()

def montar_prompt_redacao(tema: str, genero: str) -> str:
    return f"""
Atue como um professor avaliador rigoroso e empático da área de linguagens.
Leia o texto manuscrito na(s) imagem(ns) anexa(s).

Tema proposto: {tema or "(não informado)"}
Gênero textual exigido: {genero}

Comece a resposta com uma única linha exatamente neste formato:
NOME_DETECTADO: <nome que estiver escrito à mão no campo "Nome:" do cabeçalho, ou DESCONHECIDO>

Em seguida, devolva a análise em Markdown com EXATAMENTE estes tópicos:

### 1. Transcrição fiel
Transcreva o que o aluno escreveu. Marque trechos ilegíveis com [ilegível].

### 2. Análise gramatical (norma-padrão)
Aponte desvios de ortografia, concordância, regência e pontuação.

### 3. Estrutura textual e adequação ao tema
Avalie se o texto respeita a estrutura do gênero '{genero}' e se atende ao tema.

### 4. Nota sugerida
Atribua uma nota de 0 a 10, explicando brevemente o peso de cada critério.

### 5. Diagnóstico DUA
Dirija-se ao aluno pelo nome. Traga um diagnóstico pedagógico baseado no Desenho
Universal para a Aprendizagem, com 1 ou 2 intervenções práticas para superar as
barreiras de escrita identificadas.
""".strip()

def separar_nome_detectado(resposta: str) -> tuple[str, str]:
    m = re.search(r"^\s*NOME_DETECTADO:\s*(.+)$", resposta, re.MULTILINE)
    if not m:
        return "Aluno não identificado", resposta
    nome = m.group(1).strip()
    if nome.upper() in ("DESCONHECIDO", "N/A", ""):
        nome = "Aluno não identificado"
    return nome, resposta[: m.start()] + resposta[m.end():]

# ===========================================================================
# GOOGLE SHEETS
# ===========================================================================
@st.cache_resource(show_spinner=False)
def _cliente_sheets():
    if "gcp_service_account" in st.secrets:
        info = dict(st.secrets["gcp_service_account"])
        credenciais = ServiceAccountCredentials.from_service_account_info(
            info, scopes=ESCOPO_SHEETS
        )
    else:
        caminho = obter_config("GOOGLE_CREDENTIALS_PATH", "credenciais.json")
        if not os.path.exists(caminho):
            raise FileNotFoundError(
                f"Credenciais da conta de serviço não encontradas em '{caminho}'. "
                "Configure GOOGLE_CREDENTIALS_PATH ou o bloco [gcp_service_account] nos Secrets."
            )
        credenciais = ServiceAccountCredentials.from_service_account_file(
            caminho, scopes=ESCOPO_SHEETS
        )
    return gspread.authorize(credenciais)

def conectar_sheets(id_planilha: str):
    try:
        return _cliente_sheets().open_by_key(id_planilha).sheet1
    except gspread.exceptions.APIError as e:
        raise RuntimeError(
            "Não foi possível abrir a planilha. Confira o link e verifique se ela "
            "foi compartilhada com o e-mail da conta de serviço (permissão de Editor)."
        ) from e

# ===========================================================================
# INTERFACE
# ===========================================================================
carregar_gabarito_salvo()

st.title("🎓 Sistema de Avaliação Inteligente")
st.caption("Análise pedagógica, diagnósticos DUA e criação automatizada de formulários")

if st.session_state.pop("gabarito_restaurado", False):
    st.toast("Gabarito anterior carregado automaticamente.", icon="📂")

with st.sidebar:
    st.header("⚙️ Configurações")
    disciplina_escolhida = st.selectbox("Componente curricular:", DISCIPLINAS)

    st.divider()
    st.caption("Pastas do Google Drive (opcional — deixe em branco para usar a raiz)")
    entrada_pasta_provas = st.text_input(
        "Pasta das provas:", value=ID_PASTA_PROVAS,
        placeholder="Cole o link da pasta da turma...",
    )
    entrada_pasta_redacoes = st.text_input(
        "Pasta das redações:", value=ID_PASTA_REDACOES,
        placeholder="Cole o link da pasta de redações...",
    )

    st.divider()
    if "gabarito" in st.session_state:
        tipo = st.session_state.get("tipo_gabarito", "diagnostico")
        st.success(f"Gabarito em memória ({tipo}).")
    else:
        st.info("Nenhum gabarito carregado.")

id_pasta_provas = extrair_id_pasta(entrada_pasta_provas)
id_pasta_redacoes = extrair_id_pasta(entrada_pasta_redacoes)

aba_prova, aba_redacao, aba_notas = st.tabs(
    ["📄 Material e Prova", "👁️ Correção de Redações", "📊 Notas e Diagnósticos"]
)

# ---------------------------------------------------------------------------
# ABA 1 — material didático, folha de redação e geração da prova
# ---------------------------------------------------------------------------
with aba_prova:
    st.subheader("📝 Folha pautada para produção textual")
    col_tema, col_genero, col_linhas = st.columns([2, 2, 1])
    with col_tema:
        tema_redacao = st.text_input("Tema (em branco = Tema Livre):")
    with col_genero:
        genero_redacao = st.selectbox("Gênero textual:", GENEROS_TEXTUAIS)
    with col_linhas:
        num_linhas = st.number_input("Linhas:", min_value=10, max_value=30, value=20)

    st.download_button(
        "📥 Baixar folha de redação (PDF)",
        data=criar_pdf_redacao(
            disciplina_escolhida, tema_redacao, genero_redacao, int(num_linhas)
        ),
        file_name=f"Folha_Redacao_{disciplina_escolhida}.pdf",
        mime="application/pdf",
    )

    st.divider()
    st.subheader("📚 Material didático")
    # No seu file_uploader, inclua "pptx":
    materiais = st.file_uploader(
        "Escolha seus materiais (PDF, PPTX, PNG, JPG)",
        type=["pdf", "pptx", "png", "jpg", "jpeg"],
        accept_multiple_files=True,
        key="uploader_materiais",
    )

    if materiais:
        try:
            conteudo_para_ia = []
            texto_extraido_total = ""
            for arquivo in materiais:
                # Converte o nome para minúsculo para garantir a leitura correta da extensão
                nome_minusculo = arquivo.name.lower()
           
                if nome_minusculo.endswith(".pdf"):
                    leitor_pdf = PdfReader(arquivo)
                    for pagina in leitor_pdf.pages:
                        texto_pagina = pagina.extract_text()
                        if texto_pagina:
                            texto_extraido_total += texto_pagina + "\n"
                    st.success(f"✅ Arquivo PDF '{arquivo.name}' lido com sucesso!")
               
                elif nome_minusculo.endswith(".pptx"):
                    # Leitura de arquivos PPTX verificada pela extensão real do arquivo
                    apresentacao = Presentation(arquivo)
                    for slide in apresentacao.slides:
                        for shape in slide.shapes:
                            if shape.has_text_frame:
                                for paragraph in shape.text_frame.paragraphs:
                                    texto_extraido_total += paragraph.text + "\n"
                    st.success(f"✅ Apresentação PPTX '{arquivo.name}' lida com sucesso!")
               
                elif nome_minusculo.endswith((".png", ".jpg", ".jpeg")):
                    imagem = Image.open(arquivo)
                    conteudo_para_ia.append(imagem)
                    st.success(f"✅ Imagem '{arquivo.name}' carregada com sucesso!")
                    st.image(imagem, caption=f"Lido: {arquivo.name}", use_container_width=True)
               
                else:
                    st.warning(f"⚠️ O formato do arquivo '{arquivo.name}' não é suportado para leitura direta.")
                    
            if texto_extraido_total:
                conteudo_para_ia.append(texto_extraido_total)
                with st.expander("🔍 Clique para ver uma prévia de todo o texto extraído"):
                    st.text(texto_extraido_total[:1000] + ("..." if len(texto_extraido_total) > 1000 else ""))
                    
        except Exception as e:
            st.error(f"Erro durante o processamento do arquivo: {str(e)}")
    st.divider()
    st.subheader("🧠 Gerar avaliação no Google Forms")

    tipo_avaliacao = st.radio(
        "Formato da prova:",
        ["Diagnóstica (1 formulário para a turma)", "Adaptativa (4 formulários por nível)"],
        horizontal=True,
    )
    adaptativa = "Adaptativa" in tipo_avaliacao

    col_a, col_b = st.columns(2)
    with col_a:
        total_questoes = st.slider("Total de questões", 1, 10, 8)
    with col_b:
        questoes_discursivas = st.slider("Dessas, quantas discursivas", 0, total_questoes, 2)
    questoes_objetivas = total_questoes - questoes_discursivas

    st.info(
        f"Serão geradas **{questoes_objetivas} questões objetivas** e "
        f"**{questoes_discursivas} discursivas**"
        + (" para cada um dos 4 níveis." if adaptativa else ".")
    )

    if st.button(f"Gerar prova de {disciplina_escolhida}", type="primary"):
        if not materiais:
            st.warning("Envie ao menos um material didático antes de gerar a prova.")
            st.stop()

        conteudo_para_ia, aviso = preparar_conteudo_para_ia(materiais)
        if aviso:
            st.warning(aviso)
        if not conteudo_para_ia:
            st.error("Nenhum conteúdo legível foi extraído dos arquivos enviados.")
            st.stop()

        with st.spinner("A IA está formulando as questões..."):
            try:
                client = configurar_gemini()
                prompt = montar_prompt_prova(
                    disciplina_escolhida, total_questoes,
                    questoes_objetivas, questoes_discursivas, adaptativa,
                )
                resposta = gerar_com_retry(
                    client, NOME_MODELO_GEMINI, [prompt] + conteudo_para_ia
                )
            except Exception as e:
                st.error(f"Falha ao chamar a IA: {e}")
                st.stop()

        try:
            # Modo tolerante na conversão JSON para ignorar retornos de carro invisíveis
            texto_sujo = resposta.text
            match = re.search(r'(\{.*\}|\[.*\])', texto_sujo, re.DOTALL)
            texto_limpo = match.group(0) if match else texto_sujo.replace("```json", "").replace("```", "").strip()
            texto_limpo = texto_limpo.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ').replace('\\n', ' ')
            
            questoes_json = json.loads(texto_limpo, strict=False)
        except json.JSONDecodeError as e:
            st.error(f"A IA não devolveu o formato de dados esperado. {e}")
            with st.expander("Ver resposta bruta da IA"):
                st.code(texto_sujo, language="text")
            st.stop()

        tipo_gabarito = "adaptativo" if adaptativa else "diagnostico"
        st.session_state["gabarito"] = questoes_json
        st.session_state["tipo_gabarito"] = tipo_gabarito
        salvar_gabarito_em_disco(questoes_json, tipo_gabarito)

        with st.spinner("Criando o(s) formulário(s) no Google Forms..."):
            try:
                if adaptativa:
                    if not isinstance(questoes_json, dict):
                        st.error("A IA devolveu uma lista, mas o modo adaptativo exige 4 níveis.")
                        st.stop()

                    st.write("### 🔗 Avaliações adaptativas geradas")
                    for nivel, lista in questoes_json.items():
                        link = criar_formulario_ia(
                            lista, f"{disciplina_escolhida} (Nível {nivel})", id_pasta_provas
                        )
                        st.markdown(f"- **Grupo {nivel}:** [Abrir Google Forms]({link})")
                    st.success("Os 4 formulários adaptativos foram criados.")
                else:
                    if not isinstance(questoes_json, list):
                        st.error("A IA devolveu um dicionário, mas o modo diagnóstico exige uma lista.")
                        st.stop()

                    link = criar_formulario_ia(
                        questoes_json, disciplina_escolhida, id_pasta_provas
                    )
                    st.success("Avaliação e formulário criados.")
                    st.markdown(f"### 🔗 [Abrir o Google Forms]({link})")

                st.caption(
                    "Lembre-se de vincular o formulário a uma planilha de respostas "
                    "(Respostas → Vincular ao Sheets) e usar esse link na aba de notas."
                )
            except Exception as e:
                st.error(f"O gabarito foi salvo, mas houve falha ao criar o formulário: {e}")

        with st.expander("Ver gabarito oficial (JSON)"):
            st.code(json.dumps(questoes_json, ensure_ascii=False, indent=2), language="json")

# ---------------------------------------------------------------------------
# ABA 2 — correção multimodal de redações
# ---------------------------------------------------------------------------
with aba_redacao:
    st.subheader("👁️ Laboratório de letramento e correção multimodal")
    st.info("Envie a foto da redação manuscrita para transcrever, corrigir e diagnosticar.")

    fotos_redacao = st.file_uploader(
        "Foto(s) da redação manuscrita",
        type=["png", "jpg", "jpeg"],
        accept_multiple_files=True,
        key="uploader_redacao",
    )

    col_t, col_g = st.columns(2)
    with col_t:
        tema_alvo = st.text_input("Tema proposto na atividade:", key="tema_correcao")
    with col_g:
        genero_alvo = st.selectbox("Gênero cobrado:", GENEROS_TEXTUAIS, key="genero_correcao")

    if st.button("🪄 Transcrever, corrigir e diagnosticar"):
        if not fotos_redacao:
            st.warning("Envie ao menos uma foto da redação antes de analisar.")
        else:
            with st.spinner("A IA está lendo a caligrafia e elaborando o diagnóstico..."):
                try:
                    # Prepara as imagens abrindo uma por uma da lista gerada pelo uploader
                    imagens = [Image.open(foto) for foto in fotos_redacao]
                    client = configurar_gemini()
                    
                    # Junta o prompt e as imagens em uma única lista e aciona a IA com retry
                    resposta = gerar_com_retry(
                        client,
                        NOME_MODELO_GEMINI,
                        [montar_prompt_redacao(tema_alvo, genero_alvo)] + imagens,
                    )

                    # Separa o nome detectado no cabeçalho do restante do diagnóstico
                    nome_aluno, texto = separar_nome_detectado(resposta.text)
                    
                    # Salva os dados na memória do Streamlit para o botão de salvar no Docs funcionar
                    st.session_state["diagnostico_atual"] = texto.strip()
                    st.session_state["nome_aluno_redacao"] = nome_aluno
                    
                    st.success("Análise concluída.")
                except Exception as e:
                    st.error(f"Erro durante a leitura multimodal: {e}")

    if "diagnostico_atual" in st.session_state:
        st.divider()

        # --- PRÉ-VISUALIZAÇÃO MULTIMODAL ---
        if fotos_redacao:
            st.markdown("#### 🖼️ Imagem Original da Redação")
            cols = st.columns(min(len(fotos_redacao), 3) if len(fotos_redacao) > 0 else 1)
            for i, foto in enumerate(fotos_redacao):
                cols[i % 3].image(foto, use_container_width=True)
            st.divider()
        # -----------------------------------

        nome_aluno = st.text_input(
            "Nome do aluno (detectado pela IA — corrija se necessário):",
            value=st.session_state.get("nome_aluno_redacao", ""),
        )
        st.markdown(st.session_state["diagnostico_atual"])

        st.divider()
        col_salvar, col_limpar = st.columns([3, 1])

        with col_salvar:
            if st.button("📄 Salvar relatório no Google Docs"):
                with st.spinner("Criando documento no Drive..."):
                    try:
                        link_doc = criar_relatorio_google_docs(
                            nome_aluno=nome_aluno or "Aluno não identificado",
                            texto_diagnostico=st.session_state["diagnostico_atual"],
                            id_pasta_destino=id_pasta_redacoes,
                        )
                        st.success("Relatório salvo no Drive.")
                        st.markdown(f"[🔗 Abrir o Google Docs]({link_doc})")
                    except Exception as e:
                        st.error(f"Não foi possível salvar: {e}")

        with col_limpar:
            if st.button("🗑️ Limpar"):
                st.session_state.pop("diagnostico_atual", None)
                st.session_state.pop("nome_aluno_redacao", None)
                st.rerun()

# ---------------------------------------------------------------------------
# ABA 3 — notas e diagnósticos
# ---------------------------------------------------------------------------
with aba_notas:
    st.subheader("📊 Gestão de notas e diagnósticos DUA")
    st.info("Cole o link da planilha de respostas vinculada a esta avaliação.")

    entrada_planilha = st.text_input(
        "Link ou ID da planilha do Google Sheets:",
        placeholder="https://docs.google.com/spreadsheets/d/...",
    )

    col1, col2 = st.columns(2)

    with col1:
        if st.button("🚀 Processar avaliações", type="primary"):
            if not entrada_planilha:
                st.warning("Informe o link ou o ID da planilha.")
            elif "gabarito" not in st.session_state:
                st.warning("Nenhum gabarito em memória. Gere a prova na primeira aba.")
            else:
                barra = st.progress(0.0, text="Iniciando...")

                def atualizar(atual: int, total: int, nome: str) -> None:
                    barra.progress(min(atual / max(total, 1), 1.0), text=f"Corrigindo: {nome}")

                try:
                    folha = conectar_sheets(extrair_id_planilha(entrada_planilha))
                    resumo = processar_avaliacoes_personalizadas(
                        folha,
                        st.session_state["gabarito"],
                        st.session_state.get("tipo_gabarito", "diagnostico"),
                        progresso=atualizar,
                    )
                    barra.empty()
                    st.success(
                        f"✅ {resumo['corrigidos']} aluno(s) corrigido(s) · "
                        f"{resumo['ignorados']} ignorado(s) · {resumo['erros']} com erro."
                    )
                except Exception:
                    barra.empty()
                    st.error("Falha ao processar as avaliações.")
                    with st.expander("Detalhes técnicos"):
                        st.code(traceback.format_exc(), language="python")

    with col2:
        if st.button("📋 Visualizar turma"):
            if not entrada_planilha:
                st.warning("Informe o link ou o ID da planilha.")
            else:
                try:
                    folha = conectar_sheets(extrair_id_planilha(entrada_planilha))
                    dados = folha.get_all_values()

                    if len(dados) <= 1:
                        st.warning("A planilha ainda não tem respostas.")
                    else:
                        st.session_state["df_turma"] = pd.DataFrame(
                            dados[1:], columns=dados[0]
                        )
                except Exception as e:
                    st.error(f"Não foi possível ler os dados: {e}")

    if "df_turma" in st.session_state:
        df = st.session_state["df_turma"]
        st.divider()
        st.write("### 📋 Painel da turma")

        if "Conceito" in df.columns:
            conceitos = ["Excelente", "Bom", "Regular", "Baixo"]
            contagem = df["Conceito"].value_counts()

            cols = st.columns(4)
            for col, conceito in zip(cols, conceitos):
                col.metric(conceito, int(contagem.get(conceito, 0)))

            abas = st.tabs(["Todos", "Excelente 🌟", "Bom 🟢", "Regular 🟡", "Baixo 🔴"])
            with abas[0]:
                st.dataframe(df, use_container_width=True)
            for aba, conceito in zip(abas[1:], conceitos):
                with aba:
                    st.dataframe(df[df["Conceito"] == conceito], use_container_width=True)
        else:
            st.dataframe(df, use_container_width=True)
            st.caption("Processe as avaliações para ver o painel por conceito.")