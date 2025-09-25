# Desafio - Servidor Proxy Interno

Este projeto foi desenvolvido como parte da disciplina Técnicas Avançadas em Desenvolvimento de Software. O objetivo foi implementar um servidor proxy capaz de receber requisições, enfileirá-las e encaminhá-las ordenadamente ao serviço de destino, respeitando restrições de taxa e cache.

## 🎯 Requisitos atendidos
- Padrões de projeto aplicados: Command, Singleton, Decorator, Iterator, Observer.
- Observabilidade: Endpoints /metrics (Prometheus) e /health.

## 📂 Estrutura do projeto
- proxy_service.py → Implementação principal do servidor proxy.
- test_harness.py → Script para simulação de rajadas, penalidades e cache.
- requirements.txt → Dependências necessárias para rodar o projeto.

## ⚙️ Instalação e execução
🔹 Windows (PowerShell)
1. Crie o ambiente virtual:
   python -m venv .venv

2. Ative o ambiente:
   .venv\Scripts\Activate.ps1

   Se der erro de permissão, rode:
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

3. Instale as dependências:
   pip install -r requirements.txt

4. Inicie o proxy em modo simulação:
   set SIM_UPSTREAM=1
   uvicorn proxy_service:app --host 0.0.0.0 --port 8000

   Para usar o serviço real:
   set SIM_UPSTREAM=0
   uvicorn proxy_service:app --host 0.0.0.0 --port 8000
🔹 Linux / macOS (bash/zsh)
1. Crie o ambiente virtual:
   python3 -m venv .venv

2. Ative o ambiente:
   source .venv/bin/activate

3. Instale as dependências:
   pip install -r requirements.txt

4. Inicie o proxy em modo simulação:
   export SIM_UPSTREAM=1
   uvicorn proxy_service:app --host 0.0.0.0 --port 8000

   Para usar o serviço real:
   export SIM_UPSTREAM=0
   uvicorn proxy_service:app --host 0.0.0.0 --port 8000

## 🧪 Testando a aplicação
Com o proxy rodando, em outro terminal (com venv ativado):

   python test_harness.py

Resultados esperados:
- Rajada (burst test): 20 requisições enviadas em ~20 segundos (1 req/s).
- Cache test: A segunda requisição idêntica deve retornar imediatamente do cache.
- Logs e métricas em /metrics e /health.

## 🌐 Endpoints
- GET /proxy/score → Proxy para o upstream (encapsula requisições).
- GET /metrics → Métricas Prometheus.
- GET /health → Healthcheck (fila e circuito).

## ✅ Conclusão
O projeto atende aos requisitos da atividade final, com:
- Processamento ordenado de requisições.
- Tolerância a falhas e penalidades.
- Cache persistente para respostas.
- Observabilidade integrada.
