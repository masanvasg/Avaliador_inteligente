import json
import re
import streamlit as st
from pypdf import PdfReader
from PIL import Image
from avaliador import configurar_gemini, NOME_MODELO_GEMINI, processar_avaliacoes_personalizadas, extrair_json
from gerador_forms import criar_formulario_ia
import gspread
from google.oauth2.service_account import Credentials
import os
from google import genai
import time
from google.genai import errors as genai_errors
from fpdf import FPDF
import tempfile
import os

class FolhaProducao(FPDF):
    def header(self):
        # Título centralizado no topo da folha
        self.set_font("Arial", 'B', 15)
        self.cell(0, 10, "Folha Oficial de Produção Textual", border=0, ln=True, align='C')
        self.ln(5)

def criar_pdf_redacao(disciplina, tema, genero):
    pdf = FolhaProducao()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    
    # 1. Cabeçalho de Identificação do Aluno
    pdf.set_font("Arial", '', 11)
    pdf.cell(0, 8, "Nome: ________________________________________________________________________", ln=True)
    pdf.cell(0, 8, f"Componente: {disciplina}              Turma: ____________              Data: ____/____/20___", ln=True)
    pdf.ln(5)
    
    # 2. Caixa de Instruções (Fundo levemente cinza)
    pdf.set_fill_color(245, 245, 245)
    pdf.set_font("Arial", 'B', 11)
    tema_final = tema if tema.strip() else "Tema Livre"
    instrucoes = f"Tema Proposto: {tema_final}\nGênero Textual: {genero}"
    pdf.multi_cell(0, 8, instrucoes, border=1, fill=True, align='L')
    pdf.ln(10)
    
    # 3. Gerador das 20 Linhas Pautadas
    pdf.set_font("Arial", '', 10)
    pdf.set_text_color(130, 130, 130) # Cor cinza suave para os números
    
    for i in range(1, 21):
        y_atual = pdf.get_y()
        
        # Número da linha (ex: 1, 2, 3...)
        pdf.cell(8, 9, str(i), border=0, align='R')
        
        # Desenha a linha física onde o aluno vai escrever
        pdf.line(22, y_atual + 6.5, 200, y_atual + 6.5)
        pdf.ln(9.5) # Espaçamento ideal para caligrafia à mão
        
    # Salva o arquivo temporariamente para o botão de download do Streamlit
    caminho_pdf = tempfile.mktemp(suffix=".pdf")
    pdf.output(caminho_pdf)
    return caminho_pdf
# ---------------------------------------------------------------------------
# CARREGAMENTO AUTOMÁTICO DO GABARITO (sobrevive ao fechamento do navegador)
# ---------------------------------------------------------------------------
def carregar_gabarito_salvo():
    """Tenta carregar o gabarito do disco se não estiver na sessão."""
    if "gabarito" not in st.session_state:
        try:
            if os.path.exists("ultimo_gabarito.json"):
                with open("ultimo_gabarito.json", "r", encoding="utf-8") as f:
                    conteudo = f.read().strip()
                if conteudo:
                    dados = json.loads(conteudo)
                    st.session_state["gabarito"] = dados
                    
                    # Detecta se é adaptativo ou diagnóstico
                    if isinstance(dados, dict) and any(k in dados for k in ["Baixo", "Regular", "Bom", "Excelente"]):
                        st.session_state["tipo_gabarito"] = "adaptativo"
                    else:
                        st.session_state["tipo_gabarito"] = "diagnostico"
                        
                    st.toast("📂 Gabarito anterior carregado automaticamente.", icon="✅")
        except Exception:
            pass  # Se der erro no load, segue normalmente

carregar_gabarito_salvo()

def gerar_com_retry(client, model, contents, max_tentativas=3):
    """
    Tenta gerar conteúdo com retry automático e fallback de modelo.
    Estratégia: para CADA modelo da lista de fallback, tenta até max_tentativas
    vezes com backoff exponencial antes de passar para o próximo modelo.
    """
    modelos_fallback = [model, "gemini-2.0-flash", "gemini-1.5-flash"]

    ultimo_erro = None

    for modelo_atual in modelos_fallback:
        for tentativa in range(max_tentativas):
            try:
                resposta = client.models.generate_content(
                    model=modelo_atual,
                    contents=contents
                )
                return resposta  # Sucesso!

            except genai_errors.ClientError as e:
                ultimo_erro = e
                if e.status_code == 503:
                    tempo_espera = 2 ** tentativa  # 1s, 2s, 4s...
                    print(f"⚠️ Modelo {modelo_atual} indisponível (503). "
                          f"Tentativa {tentativa + 1}/{max_tentativas}. Aguardando {tempo_espera}s...")
                    time.sleep(tempo_espera)
                else:
                    raise  # Outro erro, não é 503 — não faz sentido insistir no mesmo modelo

        print(f"➡️ Esgotadas as tentativas para {modelo_atual}. Passando para o próximo modelo de fallback...")

    # Se todos os modelos e tentativas falharem
    raise Exception(
        "Servidor Gemini indisponível após várias tentativas em todos os modelos de fallback. "
        "Tente novamente em alguns minutos."
    ) from ultimo_erro


NOME_MODELO_GEMINI = "gemini-2.5-flash"  # ajuste para o modelo padrão real do seu projeto


st.set_page_config(
    page_title="Sistema de Avaliação Inteligente",
    page_icon="🎓",
    layout="centered"
)

st.title("🎓 Sistema de Avaliação Inteligente")
st.subheader("Análise Pedagógica, Diagnósticos DUA e Criação de Forms Automatizados")

# ---------------------------------------------------------------------------
# BLOCO 1 e 2: Upload Multimodal (PDFs/Imagens), Disciplinas e Geração
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

# Uploader do material didático (Passo 1) — nome de variável exclusivo
materiais_didaticos = st.file_uploader(
    "Escolha seus materiais (PDF, PNG, JPG)",
    type=["pdf", "png", "jpg", "jpeg"],
    accept_multiple_files=True,
    key="uploader_materiais_didaticos"
)

# ---------------------------------------------------------------------------
# NOVO MÓDULO: Correção Multimodal de Redações Manuscritas
# ---------------------------------------------------------------------------
st.markdown("---")
st.markdown("### 👁️ Bloco 2: Laboratório de Letramento e Correção Multimodal")
st.info("Tirou a foto da redação do aluno? Faça o upload aqui para o sistema transcrever, corrigir a gramática e avaliar a estrutura textual.")

# Uploader das fotos de redação (Bloco 2) — variável própria, usada pelo botão abaixo
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
    if foto_redacao is not None and len(foto_redacao) > 0:
        with st.spinner("A IA está lendo a caligrafia, corrigindo a gramática e elaborando o diagnóstico..."):
            try:
                from PIL import Image

                imagens_aluno = []
                for foto in foto_redacao:
                    imagens_aluno.append(Image.open(foto))

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

                # Agora usa a função de retry/fallback em vez de chamar a API diretamente
                resposta = gerar_com_retry(
                    client=cliente_genai,
                    model=NOME_MODELO_GEMINI,
                    contents=pacote_para_ia
                )

                st.success("Análise concluída com sucesso!")
                st.markdown(resposta.text)

            except Exception as e:
                st.error(f"Erro durante a leitura multimodal: {str(e)}")
    else:
        st.warning("⚠️ Por favor, faça o upload da(s) foto(s) da redação antes de clicar em analisar.")

# ---------------------------------------------------------------------------
# Extração de texto do material didático (Passo 1)
# ---------------------------------------------------------------------------
conteudo_para_ia = []
texto_extraido_total = ""

if materiais_didaticos:
    try:
        for arquivo in materiais_didaticos:
            if arquivo.type == "application/pdf":
                leitor_pdf = PdfReader(arquivo)
                for pagina in leitor_pdf.pages:
                    texto_pagina = pagina.extract_text() or ""
                    texto_extraido_total += texto_pagina + "\n"
            else:
                # Imagem (PNG/JPG) — trata como conteúdo multimodal para a IA
                from PIL import Image
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
questoes_discursivas = st.slider("Quantas dessas serão discursivas?", min_value=0, max_value=total_questoes, value=2)
questoes_objetivas = total_questoes - questoes_discursivas

st.info(f"A IA vai gerar um formulário com **{questoes_objetivas} questões objetivas** e **{questoes_discursivas} questões discursivas**.")

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
            1. Questões Objetivas (Total: {questoes_objetivas}): Formato de múltipla escolha com alternativas de A a E.
            2. Questões Discursivas (Total: {questoes_discursivas}): Formato de resposta aberta. Forneça a "pergunta" e um "criterio_correcao" (valendo no máximo 2,0 pontos).
            3. REGRA CRÍTICA DE FORMATAÇÃO: É ESTRITAMENTE PROIBIDO usar aspas duplas (" ") dentro do texto das perguntas, alternativas ou critérios. Se precisar citar, use aspas simples (' '). 
            4. NÃO use quebras de linha literais dentro dos valores JSON. Use \n se necessário.

            {instrucao_saida}
            """

            pacote_para_ia = [prompt] + conteudo_para_ia

            resposta = client.models.generate_content(
                model=NOME_MODELO_GEMINI,
                contents=pacote_para_ia
            )

            texto_resposta = resposta.text

            texto_limpo = extrair_json(texto_resposta)

            try:
                questoes_json = json.loads(texto_limpo)
            except json.JSONDecodeError as e:
                st.error(f"🚨 JSON inválido: {e}")
                st.code(texto_limpo, language="json")
                st.stop()

            st.session_state["gabarito"] = questoes_json
            st.session_state["tipo_gabarito"] = tipo_gabarito
            try:
                with open("ultimo_gabarito.json", "w", encoding="utf-8") as f:
                    json.dump(questoes_json, f, ensure_ascii=False)
            except Exception:
                pass

            if "Adaptativa" in tipo_avaliacao:
                st.write("### 🔗 Avaliações Adaptativas Geradas:")
                gabarito_adaptativo = {}

                for nivel, lista_questoes in questoes_json.items():
                    texto_nivel = json.dumps(lista_questoes, ensure_ascii=False)
                    nome_prova_nivel = f"{disciplina_escolhida} (Nível {nivel})"

                    link_nivel = criar_formulario_ia(texto_nivel, nome_prova_nivel)
                    st.markdown(f"- **Grupo {nivel}:** [Acessar Google Forms]({link_nivel})")

                    gabarito_adaptativo[nivel] = lista_questoes

                st.success("🎉 Os 4 formulários adaptativos foram gerados com sucesso!")
            else:
                link_forms = criar_formulario_ia(texto_limpo, disciplina_escolhida)

                with open("ultimo_gabarito.json", "w", encoding="utf-8") as f:
                    f.write(texto_limpo)

                st.success("🎉 Avaliação e Formulário gerados com sucesso!")
                st.markdown(f"### 🔗 [CLIQUE AQUI PARA ACESSAR O SEU GOOGLE FORMS]({link_forms})")

            with st.expander("Ver código (Gabarito Oficial)"):
                st.code(texto_limpo, language="json")

        except Exception as e:
            st.error(f"Erro durante o processamento: {str(e)}")

# ---------------------------------------------------------------------------
# BLOCO DE GESTÃO: Processamento Dinâmico com ID da Planilha na Tela
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

col1, col2 = st.columns(2)

ESCOPO_SHEETS = ["https://www.googleapis.com/auth/spreadsheets"]

def conectar_sheets(id_planilha):
    caminho_credenciais = os.environ.get("GOOGLE_CREDENTIALS_PATH", "credenciais.json")
    credenciais = Credentials.from_service_account_file(caminho_credenciais, scopes=ESCOPO_SHEETS)
    cliente = gspread.authorize(credenciais)
    return cliente.open_by_key(id_planilha).sheet1

with col1:
    if st.button("🚀 Processar Avaliações"):
        if not entrada_planilha:
            st.warning("Informe o link ou o ID da planilha.")
        elif "gabarito" not in st.session_state:
            st.warning("Gabarito não encontrado. Gere a prova no Passo 2.")
        else:
            with st.spinner("Processando..."):
                try:
                    id_limpo = extrair_id_planilha(entrada_planilha)
                    st.write(f"🔍 ID extraído: `{id_limpo}`")  # DEBUG
                    
                    caminho_credenciais = os.environ.get("GOOGLE_CREDENTIALS_PATH", "credenciais.json")
                    st.write(f"📁 Credenciais: `{caminho_credenciais}`")  # DEBUG
                
                    credenciais = Credentials.from_service_account_file(caminho_credenciais, scopes=ESCOPO_SHEETS)
                    cliente = gspread.authorize(credenciais)
                    st.write("✅ Cliente autorizado")  # DEBUG
                
                    folha = cliente.open_by_key(id_limpo).sheet1
                    st.write("✅ Planilha aberta")  # DEBUG
                
                    processar_avaliacoes_personalizadas(
                        folha,
                        st.session_state["gabarito"],
                        st.session_state.get("tipo_gabarito", "diagnostico")
                    )
                    st.success("✅ Notas e diagnósticos atualizados!")
                
                except Exception as e:
                    import traceback
                    st.error("❌ ERRO DETALHADO:")
                    st.code(traceback.format_exc(), language="python")

with col2:
    if st.button("📊 Visualizar Alunos na Planilha"):
        if not entrada_planilha:
            st.warning("Por favor, informe o link ou o ID da planilha para visualizar.")
        else:
            try:
                folha = conectar_sheets(extrair_id_planilha(entrada_planilha))
                import pandas as pd
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
