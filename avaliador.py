"""
Núcleo de IA e correção automática.

Responsabilidades:
  - configurar o cliente Gemini;
  - chamar o modelo com retry/fallback;
  - extrair JSON confiável de respostas de LLM;
  - corrigir as respostas da planilha e gravar notas, conceitos e diagnósticos DUA.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import Any, Iterable

from google import genai
from google.genai import errors as genai_errors

from config import (
    FAIXAS_CONCEITO,
    MODELOS_FALLBACK,
    NOME_MODELO_GEMINI,
    PONTOS_POR_DISCURSIVA,
    logger,
    obter_config,
)

# Colunas que este módulo cria/atualiza na planilha de respostas.
COLUNAS_RESULTADO = [
    "Nota Objetivas",
    "Nota Discursivas",
    "Nota Final (0-10)",
    "Diagnóstico DUA",
    "Conceito",
]


# ---------------------------------------------------------------------------
# Cliente Gemini
# ---------------------------------------------------------------------------
def configurar_gemini() -> genai.Client:
    """Cria o cliente da API Gemini a partir da chave em ambiente ou Secrets."""
    api_key = obter_config("GEMINI_API_KEY")

    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY não encontrada. Defina a variável de ambiente "
            "GEMINI_API_KEY ou adicione a chave em Settings > Secrets do Streamlit."
        )

    return genai.Client(api_key=api_key)


def gerar_com_retry(client, model: str, contents: Any, max_tentativas: int = 3):
    """
    Gera conteúdo com backoff exponencial e fallback entre modelos.

    Regras:
      - 503 / erro de servidor  → espera e tenta de novo no MESMO modelo;
      - 429 (cota estourada)    → troca de modelo na hora (cota é por modelo);
      - demais erros de cliente → propaga imediatamente (ex.: chave inválida).
    """
    modelos = list(dict.fromkeys([model, *MODELOS_FALLBACK]))
    ultimo_erro: Exception | None = None

    for modelo_atual in modelos:
        for tentativa in range(max_tentativas):
            try:
                return client.models.generate_content(
                    model=modelo_atual, contents=contents
                )

            except genai_errors.ServerError as e:  # 5xx
                ultimo_erro = e
                espera = 2**tentativa
                logger.warning(
                    "Modelo %s indisponível (erro de servidor). Tentativa %d/%d — aguardando %ds.",
                    modelo_atual, tentativa + 1, max_tentativas, espera,
                )
                time.sleep(espera)

            except genai_errors.ClientError as e:  # 4xx
                ultimo_erro = e
                codigo = getattr(e, "code", None)

                if codigo == 429:
                    logger.warning(
                        "Cota esgotada para %s (429). Passando para o próximo modelo.",
                        modelo_atual,
                    )
                    break

                if codigo == 503:
                    espera = 2**tentativa
                    logger.warning(
                        "Modelo %s sobrecarregado (503). Aguardando %ds.",
                        modelo_atual, espera,
                    )
                    time.sleep(espera)
                    continue

                raise  # 400, 401, 403… insistir não resolve

        logger.info("Tentativas esgotadas em %s. Tentando o próximo modelo.", modelo_atual)

    raise RuntimeError(
        "A API do Gemini não respondeu após várias tentativas em todos os modelos "
        "de fallback. Tente novamente em alguns minutos."
    ) from ultimo_erro


# ---------------------------------------------------------------------------
# Extração de JSON
# ---------------------------------------------------------------------------
_BLOCO_MD = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _escapar_quebras_dentro_de_strings(texto: str) -> str:
    """
    Escapa quebras de linha reais que estejam DENTRO de strings JSON.

    A versão anterior trocava '\\n' por espaço no texto inteiro, o que corrompia
    o conteúdo do diagnóstico. Aqui só mexemos no que está entre aspas.
    """
    resultado = []
    dentro_string = False
    escapando = False

    for char in texto:
        if escapando:
            resultado.append(char)
            escapando = False
            continue

        if char == "\\":
            resultado.append(char)
            escapando = True
            continue

        if char == '"':
            dentro_string = not dentro_string
            resultado.append(char)
            continue

        if dentro_string and char in "\n\r\t":
            resultado.append({"\n": "\\n", "\r": "", "\t": " "}[char])
            continue

        resultado.append(char)

    return "".join(resultado)


def extrair_json(texto_bruto: str) -> str:
    """
    Devolve uma string JSON válida a partir da resposta da IA.

    Estratégia em camadas:
      1. o texto já é JSON puro;
      2. está dentro de um bloco markdown ```json ... ```;
      3. está no meio de texto — recorta do primeiro '{'/'[' ao último '}'/']'.
    Em todos os casos, corrige quebras de linha cruas dentro de strings.
    """
    if not texto_bruto or not texto_bruto.strip():
        raise ValueError("A IA devolveu uma resposta vazia.")

    candidatos: list[str] = []

    bruto = texto_bruto.strip()
    candidatos.append(bruto)

    bloco = _BLOCO_MD.search(bruto)
    if bloco:
        candidatos.append(bloco.group(1).strip())

    abre = [i for i in (bruto.find("{"), bruto.find("[")) if i != -1]
    fecha = [i for i in (bruto.rfind("}"), bruto.rfind("]")) if i != -1]
    if abre and fecha and max(fecha) > min(abre):
        candidatos.append(bruto[min(abre): max(fecha) + 1])

    for candidato in candidatos:
        for tentativa in (candidato, _escapar_quebras_dentro_de_strings(candidato)):
            try:
                json.loads(tentativa)
                return tentativa
            except (json.JSONDecodeError, ValueError):
                continue

    raise ValueError("Nenhum JSON válido foi encontrado na resposta da IA.")


def carregar_json_ia(texto_bruto: str) -> Any:
    """Atalho: extrai e já devolve o objeto Python."""
    return json.loads(extrair_json(texto_bruto))


# ---------------------------------------------------------------------------
# Regras de nota e conceito
# ---------------------------------------------------------------------------
def calcular_conceito(nota: float, maximo: float) -> str:
    """
    Conceito calculado sobre o PERCENTUAL de aproveitamento.

    A versão anterior comparava a nota bruta com faixas de 0 a 10 mesmo quando
    a prova valia 8 — um aluno com 7/8 (87,5%) era classificado como 'Bom'.
    Normalizando, ele vira 'Excelente', que é o correto.
    """
    try:
        maximo = float(maximo)
        nota = float(nota)
    except (TypeError, ValueError):
        return "Indefinido"

    if maximo <= 0:
        return "Indefinido"

    percentual = max(0.0, min(100.0, (nota / maximo) * 100.0))

    for limite, conceito in FAIXAS_CONCEITO:
        if percentual <= limite:
            return conceito
    return FAIXAS_CONCEITO[-1][1]


def normalizar_para_dez(nota: float, maximo: float) -> float:
    """Converte a nota bruta para a escala 0–10, que é a que vai para a planilha."""
    if maximo <= 0:
        return 0.0
    return round((float(nota) / float(maximo)) * 10.0, 2)


def _sem_acento(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in texto if not unicodedata.combining(c)).lower().strip()


def _letra_resposta(resposta: str) -> str:
    """Extrai a letra da alternativa marcada ('C) Fotossíntese' → 'C')."""
    match = re.match(r"\s*([A-Ea-e])\s*[\)\.\-:]?", str(resposta))
    return match.group(1).upper() if match else str(resposta).strip().upper()


# ---------------------------------------------------------------------------
# Mapeamento das colunas da planilha
# ---------------------------------------------------------------------------
_PADRAO_QUESTAO = re.compile(r"^quest[ao]o?\s*(\d+)", re.IGNORECASE)


def mapear_colunas(cabecalho: list[str]) -> dict:
    """
    Descobre onde estão nome, nível e respostas — pelo texto do cabeçalho, não
    por posição fixa. Isso evita que o sistema quebre quando o professor
    acrescenta ou reordena uma pergunta no Google Forms.
    """
    mapa: dict[str, Any] = {"nome": None, "nivel": None, "respostas": []}
    numeradas: list[tuple[int, int]] = []

    for i, titulo in enumerate(cabecalho):
        limpo = _sem_acento(titulo)

        m = _PADRAO_QUESTAO.search(re.sub(r"^\d+[\.\)]\s*", "", limpo))
        if m:
            numeradas.append((int(m.group(1)), i))
            continue

        if mapa["nome"] is None and "nome" in limpo:
            mapa["nome"] = i
        elif mapa["nivel"] is None and ("nivel" in limpo or "grupo" in limpo):
            mapa["nivel"] = i

    if numeradas:
        mapa["respostas"] = [idx for _, idx in sorted(numeradas)]
    else:
        # Compatibilidade com planilhas antigas: 1 carimbo de data + 5 campos de
        # identificação antes das respostas.
        mapa["respostas"] = list(range(6, len(cabecalho)))

    return mapa


def _indice_coluna(cabecalho: list[str], nome: str) -> int:
    return cabecalho.index(nome)


def _a1(linha: int, coluna: int) -> str:
    """Converte (linha, coluna) 1-based para notação A1."""
    letras = ""
    while coluna > 0:
        coluna, resto = divmod(coluna - 1, 26)
        letras = chr(65 + resto) + letras
    return f"{letras}{linha}"


# ---------------------------------------------------------------------------
# Correção
# ---------------------------------------------------------------------------
def _montar_prompt_discursivas(
    nome_aluno: str,
    acertos_obj: int,
    total_obj: int,
    questoes_discursivas: list[dict],
    respostas: list[str],
    maximo_teorico: float,
) -> str:
    blocos = []
    for j, q in enumerate(questoes_discursivas):
        blocos.append(
            f"Questão {total_obj + j + 1}: {q.get('pergunta', '')}\n"
            f"Critério de correção: {q.get('criterio_correcao', '')}\n"
            f"Resposta do aluno: {respostas[j] if j < len(respostas) else '(em branco)'}\n"
        )

    placeholder = ", ".join(["0.0"] * len(questoes_discursivas))

    return f"""
Atue como um professor especialista, rigoroso e empático, corrigindo uma avaliação.

DADOS DO ALUNO
- Nome: {nome_aluno}
- Acertos objetivos: {acertos_obj} de {total_obj}

RESPOSTAS DISCURSIVAS (cada uma vale no máximo {PONTOS_POR_DISCURSIVA} pontos)
{"".join(blocos)}

TAREFAS
1. Atribua a cada resposta discursiva uma nota fracionada de 0.0 a {PONTOS_POR_DISCURSIVA},
   justificada pelos critérios informados.
2. Calcule a nota final somando acertos objetivos e notas discursivas.
   O máximo possível nesta prova é {maximo_teorico}.
3. Escreva um diagnóstico pedagógico DUA de 1 parágrafo, focado nas lacunas
   conceituais demonstradas, com 1 ou 2 estratégias práticas de recomposição.

FORMATO DE RETORNO (OBRIGATÓRIO)
Devolva APENAS um objeto JSON válido, sem texto antes ou depois, sem blocos de código.
Use aspas duplas nas chaves. Dentro do diagnóstico, use apenas aspas simples.

{{
  "notas_disc": [{placeholder}],
  "nota_final": 0.0,
  "diagnostico_dua": "..."
}}
""".strip()


def processar_avaliacoes_personalizadas(
    folha,
    gabarito,
    tipo_gabarito: str = "diagnostico",
    progresso=None,
) -> dict:
    """
    Corrige as respostas da planilha e grava os resultados em UMA única escrita.

    Parâmetros
    ----------
    folha : gspread.Worksheet
    gabarito : list (diagnóstico) ou dict {nível: list} (adaptativo)
    tipo_gabarito : "diagnostico" | "adaptativo"
    progresso : callable(atual, total, nome) — opcional, para a barra do Streamlit

    Retorna um resumo: {"corrigidos": n, "ignorados": n, "erros": n}
    """
    dados = folha.get_all_values()
    if len(dados) <= 1:
        logger.info("Planilha sem respostas.")
        return {"corrigidos": 0, "ignorados": 0, "erros": 0}

    cabecalho = list(dados[0])
    mapa = mapear_colunas(cabecalho)

    # Cria as colunas de resultado que ainda não existirem — em uma só chamada.
    novas = [c for c in COLUNAS_RESULTADO if c not in cabecalho]
    if novas:
        inicio = len(cabecalho) + 1
        cabecalho.extend(novas)
        folha.update(
            [novas],
            f"{_a1(1, inicio)}:{_a1(1, len(cabecalho))}",
            value_input_option="USER_ENTERED",
        )

    idx = {nome: _indice_coluna(cabecalho, nome) for nome in COLUNAS_RESULTADO}
    primeira_col_resultado = min(idx.values()) + 1
    ultima_col_resultado = max(idx.values()) + 1

    client = configurar_gemini()

    atualizacoes: list[dict] = []
    resumo = {"corrigidos": 0, "ignorados": 0, "erros": 0}
    linhas = dados[1:]

    for numero_linha, linha in enumerate(linhas, start=2):
        linha = list(linha) + [""] * (len(cabecalho) - len(linha))

        if str(linha[idx["Nota Final (0-10)"]]).strip():
            resumo["ignorados"] += 1
            continue  # já corrigido

        nome_aluno = (
            linha[mapa["nome"]].strip() if mapa["nome"] is not None else ""
        ) or "Aluno não identificado"

        if progresso:
            progresso(numero_linha - 1, len(linhas), nome_aluno)

        questoes = gabarito
        if tipo_gabarito == "adaptativo" and isinstance(gabarito, dict):
            nivel = linha[mapa["nivel"]].strip() if mapa["nivel"] is not None else ""
            if nivel not in gabarito:
                logger.warning("Nível '%s' de %s não existe no gabarito.", nivel, nome_aluno)
                resumo["ignorados"] += 1
                continue
            questoes = gabarito[nivel]

        if not isinstance(questoes, list) or not questoes:
            resumo["ignorados"] += 1
            continue

        respostas = [linha[i] for i in mapa["respostas"] if i < len(linha)]

        objetivas = [q for q in questoes if q.get("tipo") == "objetiva"]
        discursivas = [q for q in questoes if q.get("tipo") == "discursiva"]

        if not any(str(r).strip() for r in respostas):
            resumo["ignorados"] += 1
            continue

        # --- objetivas -----------------------------------------------------
        acertos = 0
        for j, q in enumerate(objetivas):
            if j >= len(respostas):
                break
            if _letra_resposta(respostas[j]) == str(q.get("correta", "")).strip().upper():
                acertos += 1

        maximo = len(objetivas) + PONTOS_POR_DISCURSIVA * len(discursivas)

        # --- só objetivas: nem chama a IA ----------------------------------
        if not discursivas:
            nota_final = normalizar_para_dez(acertos, maximo)
            atualizacoes.append({
                "range": f"{_a1(numero_linha, primeira_col_resultado)}:"
                         f"{_a1(numero_linha, ultima_col_resultado)}",
                "values": [[
                    f"{float(acertos):.1f}",
                    "0.0",
                    f"{nota_final:.2f}",
                    "Avaliação composta apenas por questões objetivas.",
                    calcular_conceito(acertos, maximo),
                ]],
            })
            resumo["corrigidos"] += 1
            continue

        # --- discursivas via IA --------------------------------------------
        respostas_disc = respostas[len(objetivas):]
        prompt = _montar_prompt_discursivas(
            nome_aluno, acertos, len(objetivas), discursivas, respostas_disc, maximo
        )

        try:
            resposta_ia = gerar_com_retry(client, NOME_MODELO_GEMINI, prompt)
            resultado = carregar_json_ia(resposta_ia.text)

            notas_disc = resultado.get("notas_disc", [])
            if not isinstance(notas_disc, Iterable) or isinstance(notas_disc, (str, bytes)):
                notas_disc = [0.0]

            soma_disc = 0.0
            for n in notas_disc:
                try:
                    soma_disc += min(PONTOS_POR_DISCURSIVA, max(0.0, float(n)))
                except (TypeError, ValueError):
                    continue

            bruta = acertos + soma_disc
            nota_final = normalizar_para_dez(bruta, maximo)
            diagnostico = str(
                resultado.get("diagnostico_dua", "Diagnóstico não gerado.")
            ).strip()

            atualizacoes.append({
                "range": f"{_a1(numero_linha, primeira_col_resultado)}:"
                         f"{_a1(numero_linha, ultima_col_resultado)}",
                "values": [[
                    f"{float(acertos):.1f}",
                    f"{soma_disc:.1f}",
                    f"{nota_final:.2f}",
                    diagnostico,
                    calcular_conceito(bruta, maximo),
                ]],
            })
            resumo["corrigidos"] += 1
            time.sleep(1.0)  # respeita o limite de requisições por minuto

        except Exception as e:  # noqa: BLE001 — um aluno com erro não pode parar a turma
            logger.exception("Falha ao corrigir a linha %d (%s).", numero_linha, nome_aluno)
            atualizacoes.append({
                "range": f"{_a1(numero_linha, idx['Diagnóstico DUA'] + 1)}",
                "values": [[f"Erro de processamento: {str(e)[:200]}"]],
            })
            resumo["erros"] += 1

    # Uma única escrita para a planilha inteira, em vez de 5 por aluno.
    if atualizacoes:
        folha.batch_update(atualizacoes, value_input_option="USER_ENTERED")

    logger.info("Correção concluída: %s", resumo)
    return resumo
