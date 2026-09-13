import json
import os
import re
import time
from google import genai
from google.genai import errors as genai_errors
import streamlit as st

NOME_MODELO_GEMINI = "gemini-2.5-flash"


def configurar_gemini():
    """
    Configura e retorna o cliente da API do Gemini (google-genai).
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        try:
            api_key = st.secrets["GEMINI_API_KEY"]
        except Exception:
            pass

    if not api_key:
        raise ValueError("GEMINI_API_KEY não encontrada. Configure a variável de ambiente ou st.secrets.")

    return genai.Client(api_key=api_key)


def gerar_com_retry(client, model, contents, max_tentativas=3):
    """
    Gera conteúdo com retry automático (backoff exponencial) e fallback de modelo.
    Estratégia: para CADA modelo da lista de fallback, tenta até max_tentativas
    vezes antes de passar para o próximo modelo.

    Única função de retry do projeto — usada tanto na geração de provas
    quanto na correção multimodal e no processamento de notas.
    """
    modelos_fallback = list(dict.fromkeys([model, "gemini-2.0-flash", "gemini-1.5-flash"]))
    ultimo_erro = None

    for modelo_atual in modelos_fallback:
        for tentativa in range(max_tentativas):
            try:
                return client.models.generate_content(model=modelo_atual, contents=contents)
            except genai_errors.ClientError as e:
                ultimo_erro = e
                # No SDK google-genai, ClientError expõe .code (int) e .status (str),
                # não .status_code.
                codigo_erro = getattr(e, "code", None)
                if codigo_erro == 503:
                    tempo_espera = 2 ** tentativa  # 1s, 2s, 4s...
                    print(f"⚠️ Modelo {modelo_atual} indisponível (503). "
                          f"Tentativa {tentativa + 1}/{max_tentativas}. Aguardando {tempo_espera}s...")
                    time.sleep(tempo_espera)
                elif codigo_erro == 429:
                    # Cota (diária ou por minuto) esgotada para este modelo especificamente.
                    # Insistir no mesmo modelo não adianta — cada modelo tem cota própria,
                    # então pula direto para o próximo da lista de fallback.
                    print(f"⚠️ Cota esgotada para {modelo_atual} (429). Pulando para o próximo modelo...")
                    break
                else:
                    raise  # Erro diferente de 503 — não insiste no mesmo modelo
        print(f"➡️ Esgotadas as tentativas para {modelo_atual}. Tentando o próximo modelo de fallback...")

    raise Exception(
        "Servidor Gemini indisponível após várias tentativas em todos os modelos de fallback. "
        "Tente novamente em alguns minutos."
    ) from ultimo_erro


def extrair_json(texto_bruto):
    """
    Extrai JSON de dentro do texto retornado pela IA.
    Aceita blocos markdown (```json ... ```) ou JSON puro, e remove
    quebras de linha/tabs que costumam invalidar o parsing.
    """
    padrao_md = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", texto_bruto)
    if padrao_md:
        bruto = padrao_md.group(1).strip()
    else:
        indices_abre = [i for i in (texto_bruto.find('{'), texto_bruto.find('[')) if i != -1]
        indices_fecha = [i for i in (texto_bruto.rfind('}'), texto_bruto.rfind(']')) if i != -1]

        if not indices_abre or not indices_fecha or max(indices_fecha) < min(indices_abre):
            raise ValueError("Nenhum JSON válido encontrado na resposta da IA.")

        bruto = texto_bruto[min(indices_abre):max(indices_fecha) + 1]

    return bruto.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ').replace('\\n', ' ').strip()


def calcular_conceito(nota, maximo):
    """
    Atribui conceito baseado na NOTA FINAL ABSOLUTA (0 a 10).
    - 0,0 a 4,0  → Baixo
    - 4,1 a 5,9  → Regular
    - 6,0 a 8,0  → Bom
    - 8,1 a 10,0 → Excelente
    """
    if maximo == 0:
        return "Indefinido"

    nota_final = float(nota)

    if nota_final <= 4.0:
        return "Baixo"
    elif nota_final <= 5.9:
        return "Regular"
    elif nota_final <= 8.0:
        return "Bom"
    else:
        return "Excelente"


def processar_avaliacoes_personalizadas(folha, gabarito, tipo_gabarito="diagnostico"):
    """
    Lê respostas do Google Forms na planilha, corrige com o gabarito
    e grava notas, diagnósticos e conceitos de volta na planilha.

    Parâmetros:
        folha: objeto worksheet do gspread
        gabarito: lista (diagnostico) ou dict {nivel: lista} (adaptativo)
        tipo_gabarito: "diagnostico" ou "adaptativo"
    """
    dados = folha.get_all_values()
    if len(dados) <= 1:
        return

    cabecalho = dados[0]

    colunas_novas = [
        "Nota Objetivas (0-6)",
        "Nota Discursivas (0-4)",
        "Nota Final (0-10)",
        "Diagnóstico DUA",
        "Conceito",
    ]
    for col in colunas_novas:
        if col not in cabecalho:
            cabecalho.append(col)
            folha.update_cell(1, len(cabecalho), col)

    idx_nota_obj = cabecalho.index("Nota Objetivas (0-6)")
    idx_nota_disc = cabecalho.index("Nota Discursivas (0-4)")
    idx_nota_final = cabecalho.index("Nota Final (0-10)")
    idx_diag = cabecalho.index("Diagnóstico DUA")
    idx_conceito = cabecalho.index("Conceito")

    OFFSET_RESPOSTAS = 6

    client = configurar_gemini()

    for i, linha in enumerate(dados[1:], start=2):

        while len(linha) < len(cabecalho):
            linha.append("")

        if str(linha[idx_nota_final]).strip() != "":
            continue

        nome_aluno = linha[1].strip() if len(linha) > 1 else "Aluno Desconhecido"
        nivel_aluno = linha[4].strip() if len(linha) > 4 else "Único"
        respostas_aluno = linha[OFFSET_RESPOSTAS:]

        if tipo_gabarito == "adaptativo" and isinstance(gabarito, dict):
            if nivel_aluno not in gabarito:
                continue
            questoes = gabarito[nivel_aluno]
        else:
            questoes = gabarito

        if not isinstance(questoes, list):
            continue

        questoes_objetivas = [q for q in questoes if q.get("tipo") == "objetiva"]
        questoes_discursivas = [q for q in questoes if q.get("tipo") == "discursiva"]
        num_questoes = len(questoes_objetivas) + len(questoes_discursivas)

        if len(respostas_aluno) < num_questoes:
            continue

        acertos_obj = 0
        for j, q_obj in enumerate(questoes_objetivas):
            if j >= len(respostas_aluno):
                break
            resp = str(respostas_aluno[j]).strip().upper()
            correta = str(q_obj.get("correta", "")).strip().upper()
            if resp.startswith(correta) or resp == correta:
                acertos_obj += 1

        if not questoes_discursivas:
            nota_final = float(acertos_obj)
            maximo_possivel = len(questoes_objetivas)
            conceito = calcular_conceito(nota_final, maximo_possivel)

            folha.update_cell(i, idx_nota_obj + 1, str(float(acertos_obj)))
            folha.update_cell(i, idx_nota_disc + 1, "0.0")
            folha.update_cell(i, idx_nota_final + 1, str(nota_final))
            folha.update_cell(i, idx_diag + 1, "Sem questões discursivas.")
            folha.update_cell(i, idx_conceito + 1, conceito)
            continue

        prompt_discursivas = []
        for j, q_disc in enumerate(questoes_discursivas):
            idx_resposta = len(questoes_objetivas) + j
            resp_texto = respostas_aluno[idx_resposta] if idx_resposta < len(respostas_aluno) else ""
            prompt_discursivas.append(
                f"Questão {len(questoes_objetivas) + j + 1}: {q_disc.get('pergunta', '')}\n"
                f"Critério de Correção: {q_disc.get('criterio_correcao', '')}\n"
                f"Resposta do Aluno: {resp_texto}\n"
            )

        bloco_discursivas = "\n".join(prompt_discursivas)
        notas_placeholder = ", ".join(["0.0"] * len(questoes_discursivas))
        maximo_teorico = len(questoes_objetivas) + (2.0 * len(questoes_discursivas))

        prompt_avaliacao = f"""
Atue como um professor especialista rigoroso corrigindo uma avaliação.

DADOS DO ALUNO:
- Nome: {nome_aluno}
- Acertos Objetivos: {acertos_obj} / {len(questoes_objetivas)}

AVALIAÇÃO DAS DISCURSIVAS (Nota Máxima: 2,0 valores cada):
{bloco_discursivas}

TAREFA OBRIGATÓRIA:
1. Analise cada resposta discursiva com base nos critérios e atribua notas fracionadas de 0.0 a 2.0.
2. Calcule a Nota Final (Acertos Objetivos + Soma das Discursivas). O máximo possível é {maximo_teorico}.
3. Elabore um Diagnóstico Pedagógico DUA de 1 parágrafo focado nas lacunas conceituais demonstradas, sugerindo estratégias práticas de recomposição.

REGRA DE RETORNO (BLINDADA):
Devolva APENAS um JSON EXATO e estritamente válido.
- Use aspas duplas (") OBRIGATORIAMENTE para as chaves do JSON.
- Se precisar destacar palavras DENTRO do texto do diagnóstico, use EXCLUSIVAMENTE aspas simples (').

Formato esperado:
{{
  "notas_disc": [{notas_placeholder}],
  "nota_final": 0.0,
  "diagnostico_dua": "O aluno compreendeu o conceito base..."
}}
"""

        try:
            resposta_ia = gerar_com_retry(client, NOME_MODELO_GEMINI, prompt_avaliacao)

            texto_limpo = extrair_json(resposta_ia.text)
            resultado_json = json.loads(texto_limpo, strict=False)

            notas_disc = resultado_json.get("notas_disc", [0.0] * len(questoes_discursivas))
            if not isinstance(notas_disc, list):
                notas_disc = [0.0]

            soma_disc = sum(float(n) for n in notas_disc)
            nota_final = float(resultado_json.get("nota_final", acertos_obj + soma_disc))
            diagnostico = resultado_json.get("diagnostico_dua", "Diagnóstico não gerado.")

            maximo_possivel = len(questoes_objetivas) + (2.0 * len(questoes_discursivas))
            conceito = calcular_conceito(nota_final, maximo_possivel)

            folha.update_cell(i, idx_nota_obj + 1, str(float(acertos_obj)))
            folha.update_cell(i, idx_nota_disc + 1, str(soma_disc))
            folha.update_cell(i, idx_nota_final + 1, str(nota_final))
            folha.update_cell(i, idx_diag + 1, diagnostico)
            folha.update_cell(i, idx_conceito + 1, conceito)

            time.sleep(1.5)

        except Exception as e:
            folha.update_cell(i, idx_diag + 1, f"Erro de processamento IA: {str(e)[:200]}")
