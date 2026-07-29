# Total Solutions CRM

CRM operacional para atendimento, agenda, cotacoes, execucao de servicos, evidencias, garantias e pos-venda da Total Solutions em Cancun.

## Rodar local

Crie uma `.env` a partir de `.env.example` e configure uma `DATABASE_URL` propria do Total Solutions CRM.

```bash
cd /Users/user/TotalSolutions_CRM
source venv/bin/activate
cd backend
uvicorn app.main:app --reload
```

Abra:

```text
http://127.0.0.1:8010
```

## Variaveis de ambiente

```bash
DATABASE_URL=postgresql://user:password@host:5432/totalsolutions_crm
ROOT_KEY=sua-chave-root
MATRIX_IMPORT_TOKEN=token-seguro-para-integracao
ROOT_USERNAME=root
ROOT_PASSWORD=sua-senha-root
ROOT_FULL_NAME=Administrador Total Solutions
JWT_SECRET_KEY=chave-longa-e-aleatoria
JWT_ALGORITHM=HS256
JWT_EXPIRE_HOURS=12
ALLOW_LEGACY_ACTOR_HEADER=false
PUBLIC_BASE_URL=https://seu-dominio-total-solutions
```

## Regra de independencia

Este projeto deve usar banco de dados, usuarios, uploads, variaveis de ambiente, Railway e dominio independentes. Nenhum dado operacional deve ser compartilhado automaticamente com outro CRM.

Por seguranca, scripts de importacao exigem que o arquivo de origem seja informado explicitamente.

## Deploy Railway

1. Suba este projeto para um repositorio GitHub proprio.
2. No Railway, crie um projeto novo.
3. Adicione um servico PostgreSQL novo.
4. Adicione um servico web pelo GitHub apontando para este repositorio.
5. Configure `DATABASE_URL` com a URL do PostgreSQL novo.
6. Configure `ROOT_KEY`, `JWT_SECRET_KEY`, `ROOT_USERNAME`, `ROOT_PASSWORD` e `ROOT_FULL_NAME`.
7. Configure `PUBLIC_BASE_URL` com a URL publica ou dominio do Total Solutions CRM.
8. O comando de start esta em `railway.toml`.

Depois do deploy, abra a URL publica e entre com o root criado automaticamente no primeiro start.

## Importacao explicita

Para importar leads de um SQLite aprovado:

```bash
cd /Users/user/TotalSolutions_CRM/backend
source ../venv/bin/activate
DATABASE_URL="cole-a-url-postgres-da-railway" python scripts/import_sqlite_leads.py --source-db "/caminho/para/origem.db"
```

Para comparar antes de aplicar:

```bash
DATABASE_URL="cole-a-url-postgres-da-railway" python scripts/compare_sqlite_incremental.py --source-db "/caminho/para/origem.db"
```

## Endpoint de importacao

```text
POST /imports/matrix/leads
Authorization: Bearer <MATRIX_IMPORT_TOKEN>
Content-Type: application/json
```

Exemplo:

```json
{
  "source": "TotalSolutions_Import",
  "batch_id": "ts-2026-07-27-001",
  "sent_at": "2026-07-27T18:00:00Z",
  "records": [
    {
      "nome": "Cliente ABC",
      "contato": "+52 998 123 4567",
      "email": "cliente@example.com",
      "endereco": "Cancun, Quintana Roo",
      "nicho": "Mantenimiento residencial",
      "pais": "MX",
      "score": 80
    }
  ]
}
```

Logs:

- `GET /imports/jobs`
- `GET /imports/jobs/{job_id}`

## Fluxo inicial

1. Root entra no sistema.
2. Root cria usuarios de atendimento, gerente ou tecnico/comercial.
3. O time registra contatos, visitas e cotacoes.
4. A operacao acompanha servicos por etapa.
5. Evidencias, documentos, garantias e retornos ficam no historico do cliente.
