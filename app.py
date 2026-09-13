import json
import os
import tempfile
import traceback

import gspread
import pandas as pd
import streamlit as st
from fpdf import FPDF
from PIL import Image
from pypdf import PdfReader
from google.oauth2.service_account import Credentials as ServiceAccountCredentials

from avaliador import (
    NOME_MODELO_GEMINI,
    configurar_gemini,
    extrair_json,
    gerar_com_retry,
    processar_avaliacoes_personalizadas,
)
from gerador_forms import criar_formulario_ia


# ---------------------------------------------------------------------------
# GERAÇÃO DA FOLHA DE REDAÇÃO EM PDF
# ---------------------------------------------------------------------------
class FolhaProducao(FPDF):
    def header(self):
        self.set_font("Arial", 'B', 15)
        self.cell(0, 10, "Folha Oficial de Produção Textual", border=0, ln=True, align='C')
        self.ln(5)


def criar_pdf_redacao(disciplina, tema, genero):
    pdf = FolhaProducao()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.set_font("Arial", '', 11)
    pdf.cell(0, 8, "Nome: ________________________________________________________________________", ln=True)
    pdf.cell(0, 8, f"Componente: {disciplina}              Turma: ____________              Data: ____/____/20___", ln=True)
    pdf.ln(5)

    pdf.set_fill_color(245, 245, 245)
    pdf.set_font("Arial", 'B', 11)
    tema_final = tema if tema.strip() else "Tema Livre"
    instrucoes = f"Tema Proposto: {tema_final}\nGênero Textual: {genero}"
    pdf.multi_cell(0, 8, instrucoes, border=1, fill=True, align='L')
    pdf.ln(10)

    pdf.set_font("Arial", '', 10)
    pdf.set_text_color(130, 130, 130)

    for i in range(1, 21):
        y_atual = pdf.get_y()
        pdf.cell(8, 9, str(i), border=0, align='R')
        pdf.line(22, y_atual + 6.5, 200, y_atual + 6.5)
        pdf.ln(9.5)

    caminho_pdf = tempfile.mktemp(suffix=".pdf")
    pdf.output(caminho_pdf)
    return caminho_pdf


# ---------------------------------------------------------------------------
# CARREGAMENTO AUTOMÁTICO DO GABARITO (sobrevive ao fechamento do navegador)
# ---------------------------------------------------------------------------
def carregar_gabarito_salvo():
    """Tenta carregar o gabarito do disco se não estiver na sessão."""
    if "gabarito" in st.session_state:
        return
    try:
        if os.path.exists("ultimo_gabarito.json"):
            with open("ultimo_gabarito.json", "r", encoding="utf-8") as f:
                conteudo = f.read().strip()
            if conteudo:
                dados = json.loads(conteudo)
                st.session_state["gabarito"] = dados
                if isinstance(dados, dict) and any(k in dados for k in ["Baixo", "Regular", "Bom", "Excelente"]):
                    st.session_state["tipo_gabarito"] = "adaptativo"
                else:
                    st.session_state["tipo_gabarito"] = "diagnostico"
                st.toast("📂 Gabarito anterior carregado automaticamente.", icon="✅")
    except Exception:
        pass


def salvar_gabarito_em_disco(gabarito):
    try:
        with open("ultimo_gabarito.json", "w", encoding="utf-8") as f:
            json.dump(gabarito, f, ensure_ascii=False)
    except Exception:
        pass


carregar_gabarito_salvo()


st.set_page_config(
    page_title="Sistema de Avaliação Inteligente",
    page_icon="🎓",
    layout="centered"
)

st.title("🎓 Sistema de Avaliação Inteligente")
st.subheader("Análise Pedagógica, Diagnósticos DUA e Criação de Forms Automatizados")

# ---------------------------------------------------------------------------
# PASSO 1: Upload Multimodal (PDFs/Imagens), Disciplinas
# ---------------------------------------------------------------------------
st.markdown("### 📄 Passo 1: Enviar Material Didático e Configurar")
st.write("Faça o upload dos seus materiais (PDFs ou Imagens) e selecione a disciplina para criar avaliações personalizadas.")

lista_disciplinas = [
    "Ciências", "Biologia", "Física", "Química", "Geografia",
    "História", "Sociologia", "Filosofia", "Inglês", "Espanhol",
    "Ed. Financeira", "Ed. Digital", "Ed. Ambiental", "Sustentabilidade",
    "Matemática", "Português", "Robótica", "Programação", "Ed. Física", "Artes"
]
disciplina_escolhida = st.selectbox("Selecione o Componente Curricular:", lista_disciplinas)

st.markdown("---")
st.markdown("### 📝 Módulo de Produção Textual")
st.info("Gere uma folha pautada estruturada para avaliação manuscrita.")

col_tema, col_genero = st.columns(2)
with col_tema:
    tema_redacao = st.text_input("Tema da Produção (deixe em branco para Tema Livre):")
with col_genero:
    lista_generos = ["Dissertação-Argumentativa", "Relatório Técnico", "Artigo de Opinião", "Crônica", "Carta Aberta", "Texto Livre"]
    genero_redacao = st.selectbox("Gênero Textual:", lista_generos)

if st.button("📄 Gerar Folha em PDF para Impressão"):
    with st.spinner("Desenhando a folha pautada..."):
        caminho_arquivo = criar_pdf_redacao(disciplina_escolhida, tema_redacao, genero_redacao)
        with open(caminho_arquivo, "rb") as f:
            pdf_bytes = f.read()
        st.success("Folha gerada com sucesso! Clique abaixo para baixar e imprimir.")
        st.download_button(
            label="📥 Baixar Folha de Redação (PDF)",
            data=pdf_bytes,
            file_name=f"Folha_Redacao_{disciplina_escolhida}.pdf",
            mime="application/pdf"
        )

st.markdown("---")

materiais_didaticos = st.file_uploader(
    "Escolha seus materiais (PDF, PNG, JPG)",
    type=["pdf", "png", "jpg", "jpeg"],
    accept_multiple_files=True,
    key="uploader_materiais_didaticos"
)

# ---------------------------------------------------------------------------
# BLOCO 2: Correção Multimodal de Redações Manuscritas
# ---------------------------------------------------------------------------
st.markdown("---")
st.markdown("### 👁️ Bloco 2: Laboratório de Letramento e Correção Multimodal")
st.info("Tirou a foto da redação do aluno? Faça o upload aqui para o sistema transcrever, corrigir a gramática e avaliar a estrutura textual.")

foto_redacao = st.file_uploader(
    "Escolha a(s) foto(s) da redação manuscrita",
    type=["pdf", "png", "jpg", "jpeg"],
    accept_multiple_files=True,
    key="uploader_foto_redacao"
)

col_tema_corr, col_gen_corr = st.columns(2)
with col_tema_corr:
    tema_alvo = st.text_input("Tema proposto na atividade (ex: Segurança no Trabalho):")
with col_gen_corr:
    genero_alvo = st.selectbox("Gênero Textual cobrado:", ["Dissertação-Argumentativa", "Relatório Técnico", "Crônica", "Artigo de Opinião", "Texto Livre"])

if st.button("🪄 Transcrever, Corrigir e Diagnosticar"):
    if foto_redacao:
        with st.spinner("A IA está lendo a caligrafia, corrigindo a gramática e elaborando o diagnóstico..."):
            try:
                imagens_aluno = [Image.open(foto) for foto in foto_redacao]
                cliente_genai = configurar_gemini()

                prompt_correcao = f"""
                Atue como um professor avaliador rigoroso e empático de linguagens.
                Leia o texto manuscrito na(s) imagem(ns) anexa(s).

                Tema proposto ao aluno: {tema_alvo}
                Gênero Textual exigido: {genero_alvo}

                Devolva uma análise estruturada em Markdown contendo EXATAMENTE estes tópicos:

                ### 📝 1. Transcrição Fiel
                (Transcreva o que o aluno escreveu. Se alguma palavra estiver totalmente ilegível, coloque [ilegível]).

                ### 🚨 2. Análise Gramatical (Norma-Padrão)
                (Aponte com clareza os desvios de ortografia, concordância, regência ou pontuação).

                ### 🏗️ 3. Análise de Estrutura Textual e Tema
                (Avalie se o texto respeita a estrutura do gênero '{genero_alvo}' e se abordou adequadamente o tema proposto).

                ### 📊 4. Nota Sugerida
                (Atribua uma nota justa de 0 a 10 baseada nos critérios acima, explicando rapidamente o peso).

                ### 🧠 5. Diagnóstico DUA e Intervenção
                (Forneça um diagnóstico pedagógico estruturado no Desenho Universal para a Aprendizagem. Sugira 1 ou 2 intervenções práticas para ajudar este aluno específico a superar as barreiras de escrita identificadas).
                """

                pacote_para_ia = [prompt_correcao] + imagens_aluno
                resposta = gerar_com_retry(cliente_genai, NOME_MODELO_GEMINI, pacote_para_ia)

                st.success("Análise concluída com sucesso!")
                st.markdown(resposta.text)

            except Exception as e:
                st.error(f"Erro durante a leitura multimodal: {str(e)}")
    else:
        st.warning("⚠️ Por favor, faça o upload da(s) foto(s) da redação antes de clicar em analisar.")

# ---------------------------------------------------------------------------
# Extração de texto/imagens do material didático (Passo 1)
# ---------------------------------------------------------------------------
conteudo_para_ia = []
texto_extraido_total = ""

if materiais_didaticos:
    try:
        for arquivo in materiais_didaticos:
            if arquivo.type == "application/pdf":
                leitor_pdf = PdfReader(arquivo)
                for pagina in leitor_pdf.pages:
                    texto_extraido_total += (pagina.extract_text() or "") + "\n"
            else:
                conteudo_para_ia.append(Image.open(arquivo))

        if texto_extraido_total:
            conteudo_para_ia.append(texto_extraido_total)

    except Exception as e:
        st.error(f"Erro ao processar os materiais didáticos: {str(e)}")

st.markdown("### 🧠 Passo 2: Gerar Avaliação e Formulário")
st.write("#### ⚙️ Configuração da Avaliação")

tipo_avaliacao = st.radio(
    "Selecione o formato da prova:",
    ["Diagnóstica (1 Formulário para a turma toda)", "Adaptativa (4 Formulários por Nível)"],
    horizontal=True
)
st.write("---")

total_questoes = st.slider("Total de questões da avaliação", min_value=1, max_value=10, value=8)
questoes_discursivas_n = st.slider("Quantas dessas serão discursivas?", min_value=0, max_value=total_questoes, value=2)
questoes_objetivas_n = total_questoes - questoes_discursivas_n

st.info(f"A IA vai gerar um formulário com **{questoes_objetivas_n} questões objetivas** e **{questoes_discursivas_n} questões discursivas**.")

if st.button(f"Gerar Prova de {disciplina_escolhida} no Google Forms", type="primary"):
    with st.spinner("A IA está analisando os materiais, formulando as questões e construindo o Google Forms..."):
        try:
            client = configurar_gemini()

            regra_interdisciplinar = ""
            if disciplina_escolhida not in ["Português", "Matemática"]:
                regra_interdisciplinar = """
                REGRA DE INTERDISCIPLINARIDADE: As questões devem instigar o raciocínio crítico.
                Sempre que o contexto permitir, integre a aplicação de raciocínio lógico-matemático
                ou exija uma interpretação de texto aprofundada.
                """

            if "Adaptativa" in tipo_avaliacao:
                instrucao_saida = f"""
                Crie 4 provas diferentes a partir deste material (cada uma contendo exatamente {total_questoes} questões).
                Ajuste o nível cognitivo das questões para cada grupo:
                - 'Baixo' (questões diretas e básicas), 'Regular' (intermediárias), 'Bom' (análise e relação) e 'Excelente' (alta complexidade e síntese).
                DEVOLUÇÃO OBRIGATÓRIA: Devolva um único arquivo JSON no formato de DICIONÁRIO contendo 4 listas:
                {{
                    "Baixo": [ {{"tipo": "objetiva", "pergunta": "...", "A": "...", "correta": "C"}} ],
                    "Regular": [ ... ],
                    "Bom": [ ... ],
                    "Excelente": [ ... ]
                }}
                """
                tipo_gabarito = "adaptativo"
            else:
                instrucao_saida = f"""
                Crie uma avaliação única e equilibrada para toda a turma (contendo exatamente {total_questoes} questões).
                DEVOLUÇÃO OBRIGATÓRIA: Devolva a prova EXATAMENTE no formato JSON de LISTA, como no exemplo abaixo:
                [
                    {{"tipo": "objetiva", "pergunta": "...", "A": "...", "correta": "C"}}
                ]
                """
                tipo_gabarito = "diagnostico"

            prompt = f"""
            Atue como um professor especialista em {disciplina_escolhida}.
            Leia o material de apoio fornecido e construa a(s) avaliação(ões).
            {regra_interdisciplinar}

            REGRAS DE ESTRUTURAÇÃO (Para CADA prova gerada):
            1. Questões Objetivas (Total: {questoes_objetivas_n}): Formato de múltipla escolha com alternativas de A a E.
            2. Questões Discursivas (Total: {questoes_discursivas_n}): Formato de resposta aberta. Forneça a "pergunta" e um "criterio_correcao" (valendo no máximo 2,0 pontos).
            3. REGRA CRÍTICA DE FORMATAÇÃO: É ESTRITAMENTE PROIBIDO usar aspas duplas (" ") dentro do texto das perguntas, alternativas ou critérios. Se precisar citar, use aspas simples (' ').
            4. NÃO use quebras de linha literais dentro dos valores JSON. Use \\n se necessário.

            {instrucao_saida}
            """

            pacote_para_ia = [prompt] + conteudo_para_ia
            resposta = gerar_com_retry(client, NOME_MODELO_GEMINI, pacote_para_ia)

            try:
                texto_limpo = extrair_json(resposta.text)
                questoes_json = json.loads(texto_limpo, strict=False)
            except (ValueError, json.JSONDecodeError) as e:
                st.error(f"🚨 A IA não retornou o formato de dados corretamente! Erro: {e}")
                st.info("Veja abaixo a resposta bruta da IA para análise:")
                st.code(resposta.text, language="text")
                st.stop()

            st.session_state["gabarito"] = questoes_json
            st.session_state["tipo_gabarito"] = tipo_gabarito
            salvar_gabarito_em_disco(questoes_json)

            if "Adaptativa" in tipo_avaliacao:
                st.write("### 🔗 Avaliações Adaptativas Geradas:")
                for nivel, lista_questoes in questoes_json.items():
                    texto_nivel = json.dumps(lista_questoes, ensure_ascii=False)
                    nome_prova_nivel = f"{disciplina_escolhida} (Nível {nivel})"
                    link_nivel = criar_formulario_ia(texto_nivel, nome_prova_nivel)
                    st.markdown(f"- **Grupo {nivel}:** [Acessar Google Forms]({link_nivel})")
                st.success("🎉 Os 4 formulários adaptativos foram gerados com sucesso!")
            else:
                link_forms = criar_formulario_ia(texto_limpo, disciplina_escolhida)
                st.success("🎉 Avaliação e Formulário gerados com sucesso!")
                st.markdown(f"### 🔗 [CLIQUE AQUI PARA ACESSAR O SEU GOOGLE FORMS]({link_forms})")

            with st.expander("Ver código (Gabarito Oficial)"):
                st.code(texto_limpo, language="json")

        except Exception as e:
            st.error(f"Erro durante o processamento: {str(e)}")

# ---------------------------------------------------------------------------
# PASSO 3: Gestão de Notas e Diagnósticos DUA
# ---------------------------------------------------------------------------
st.markdown("---")
st.markdown("### 📊 Passo 3: Gestão de Notas e Diagnósticos DUA")
st.info("💡 Cole abaixo o link ou o ID da planilha exclusiva gerada por esta avaliação para processar as notas e gerar as intervenções baseadas no DUA.")

entrada_planilha = st.text_input("Link ou ID da Planilha do Google Sheets:", placeholder="Cole aqui o link ou o ID da planilha...")


def extrair_id_planilha(texto):
    if "spreadsheets/d/" in texto:
        partes = texto.split("spreadsheets/d/")
        if len(partes) > 1:
            return partes[1].split("/")[0]
    return texto.strip()


ESCOPO_SHEETS = ["https://www.googleapis.com/auth/spreadsheets"]


def conectar_sheets(id_planilha):
    # No Streamlit Community Cloud não existe arquivo local de credenciais —
    # nesse caso, o service account é lido dos Secrets do app
    # (bloco [gcp_service_account] em Settings > Secrets).
    if "gcp_service_account" in st.secrets:
        info = dict(st.secrets["gcp_service_account"])
        credenciais = ServiceAccountCredentials.from_service_account_info(info, scopes=ESCOPO_SHEETS)
    else:
        caminho_credenciais = os.environ.get("GOOGLE_CREDENTIALS_PATH", "credenciais.json")
        credenciais = ServiceAccountCredentials.from_service_account_file(caminho_credenciais, scopes=ESCOPO_SHEETS)

    cliente = gspread.authorize(credenciais)
    return cliente.open_by_key(id_planilha).sheet1


col1, col2 = st.columns(2)

with col1:
    if st.button("🚀 Processar Avaliações"):
        if not entrada_planilha:
            st.warning("Informe o link ou o ID da planilha.")
        elif "gabarito" not in st.session_state:
            st.warning("Gabarito não encontrado. Gere a prova no Passo 2.")
        else:
            with st.spinner("Processando..."):
                try:
                    folha = conectar_sheets(extrair_id_planilha(entrada_planilha))
                    processar_avaliacoes_personalizadas(
                        folha,
                        st.session_state["gabarito"],
                        st.session_state.get("tipo_gabarito", "diagnostico")
                    )
                    st.success("✅ Notas e diagnósticos atualizados!")
                except Exception:
                    st.error("❌ ERRO DETALHADO:")
                    st.code(traceback.format_exc(), language="python")

with col2:
    if st.button("📊 Visualizar Alunos na Planilha"):
        if not entrada_planilha:
            st.warning("Por favor, informe o link ou o ID da planilha para visualizar.")
        else:
            try:
                folha = conectar_sheets(extrair_id_planilha(entrada_planilha))
                dados_brutos = folha.get_all_values()

                if len(dados_brutos) > 0:
                    cabecalho = dados_brutos[0]
                    linhas = dados_brutos[1:]
                    df = pd.DataFrame(linhas, columns=cabecalho)

                    st.write("### 📋 Painel de Controle: Dados Atuais da Turma")
                    nome_coluna = "Conceito"

                    if nome_coluna in df.columns:
                        aba_todos, aba_excelente, aba_bom, aba_regular, aba_baixo = st.tabs(
                            ["Todos os Alunos", "Excelente 🌟", "Bom 🟢", "Regular 🟡", "Baixo 🔴"]
                        )
                        with aba_todos:
                            st.dataframe(df)
                        with aba_excelente:
                            st.dataframe(df[df[nome_coluna] == "Excelente"])
                        with aba_bom:
                            st.dataframe(df[df[nome_coluna] == "Bom"])
                        with aba_regular:
                            st.dataframe(df[df[nome_coluna] == "Regular"])
                        with aba_baixo:
                            st.dataframe(df[df[nome_coluna] == "Baixo"])
                    else:
                        st.dataframe(df)
                else:
                    st.warning("A planilha parece estar vazia ou sem cabeçalhos válidos.")
            except Exception as e:
                st.error(f"Não foi possível ler os dados: {str(e)}")
