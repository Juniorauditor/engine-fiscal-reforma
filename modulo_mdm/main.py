from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from datetime import date
import redis
import json
import logging
import os
import psycopg2 # Adicionado para conectar ao Postgres real do Railway
from psycopg2.extras import RealDictCursor

# Configuração de Logs para monitoramento da API
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Modulo_MDM")

app = FastAPI(
    title="Módulo 1: Cadastro Base e Matriz De-Para",
    description="API para resolução de alíquotas híbridas e saneamento de dados da Reforma Tributária",
    version="1.0.0"
)

# --- CONFIGURAÇÃO DE AMBIENTE (RAILWAY) ---
# O Railway injeta essas variáveis automaticamente no container da sua aplicação
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
DATABASE_URL = os.environ.get("DATABASE_URL")

# Conexão com o Redis (Cache na Nuvem)
try:
    cache = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    logger.info("Conexão com o Redis estabelecida com sucesso!")
except Exception as e:
    logger.warning(f"Não foi possível conectar ao Redis: {e}")
    cache = None

# --- MODELAGEM DE DADOS (Pydantic) ---
class RegraFiscalResponse(BaseModel):
    codigo_interno: str
    cClassTrib_novo: str
    uf_destino: str
    data_consulta: date
    aliquota_cbs: float
    aliquota_ibs: float
    aliquota_is: float
    aliquota_icms_legado: float
    aliquota_iss_legado: float
    origem_dados: str 

# --- CONEXÃO COM BANCO DE DADOS REAL (PostgreSQL) ---
def buscar_regras_no_banco(codigo_produto: str, uf: str, data_atual: date) -> dict:
    """
    Consulta a matriz de alíquotas híbridas diretamente nas tabelas reais do Postgres.
    """
    if not DATABASE_URL:
        logger.error("DATABASE_URL não configurada. Usando modo de segurança.")
        return None

    query = """
        SELECT 
            p.codigo_interno, p.cClassTrib_novo, a.uf_destino,
            a.aliquota_cbs, a.aliquota_ibs, a.aliquota_is,
            a.aliquota_icms_legado, a.aliquota_iss_legado
        FROM tb_produto p
        JOIN tb_aliquota_transicao a ON p.cClassTrib_novo = a.cClassTrib
        WHERE p.codigo_interno = %s 
          AND a.uf_destino = %s
          AND %s BETWEEN a.data_inicio AND a.data_fim;
    """
    try:
        # Abre conexão com as credenciais do Railway
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute(query, (codigo_produto, uf.upper(), data_atual))
        resultado = cursor.fetchone()
        
        cursor.close()
        conn.close()
        
        if resultado:
            resultado["data_consulta"] = data_atual
            return resultado
        return None
    except Exception as e:
        logger.error(f"Erro ao consultar o PostgreSQL: {e}")
        return None

# --- ENDPOINTS / ROTAS DA API ---
@app.get("/api/v1/aliquota/resolver", response_model=RegraFiscalResponse)
def resolver_aliquota(
    codigo_produto: str = Query(..., description="Código SKU interno do produto/serviço no ERP"),
    uf_destino: str = Query(..., min_length=2, max_length=2, description="UF de consumo (Princípio do Destino)")
):
    hoje = date.today()
    cache_key = f"aliq:{codigo_produto}:{uf_destino.upper()}:{hoje.isoformat()}"
    
    # 1. Tenta recuperar do Cache (Redis)
    if cache:
        try:
            cached_data = cache.get(cache_key)
            if cached_data:
                logger.info(f"Cache HIT para a chave: {cache_key}")
                dados = json.loads(cached_data)
                return dados
        except Exception as e:
            logger.error(f"Falha ao ler cache do Redis: {e}")

    # 2. Cache MISS: Busca no Banco de Dados Real (PostgreSQL)
    logger.info(f"Cache MISS para a chave: {cache_key}. Buscando no Postgres...")
    dados_fiscais = buscar_regras_no_banco(codigo_produto, uf_destino, hoje)
    
    if not dados_fiscais:
        raise HTTPException(
            status_code=404, 
            detail=f"Regra fiscal não localizada para o produto '{codigo_produto}' com destino a UF '{uf_destino}'."
        )
        
    # 3. Grava o resultado no Cache com TTL de 1 hora
    if cache:
        try:
            dados_para_cache = dict(dados_fiscais)
            dados_para_cache["data_consulta"] = dados_para_cache["data_consulta"].isoformat()
            dados_para_cache["origem_dados"] = "CACHE"
            cache.setex(cache_key, 3600, json.dumps(dados_para_cache))
        except Exception as e:
            logger.error(f"Falha ao salvar dados no Redis: {e}")

    dados_fiscais["origem_dados"] = "BANCO_DE_DADOS"
    return dados_fiscais

@app.get("/health")
def health_check():
    redis_status = "UP" if cache and cache.ping() else "DOWN"
    return {"status": "healthy", "redis_integration": redis_status}

