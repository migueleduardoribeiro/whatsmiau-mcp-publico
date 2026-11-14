# WhatsMiau: painel e camada MCP

Projeto pessoal que conecta assistentes de IA ao WhatsApp por meio de uma camada MCP desenvolvida sobre a API [WhatsMiau](https://github.com/verbeux-ai/whatsmiau). Inclui também um painel para administrar instâncias, conexão e webhooks.

O MCP oferece busca de contatos e grupos, consulta de mensagens arquivadas e envio de textos e arquivos. Há ainda recursos opcionais, como transcrição e análise de imagens, sujeitos a configuração e serviços externos.

Esta é uma cópia de apresentação, publicada somente para leitura, com exemplos e dados de infraestrutura substituídos. Foi desenvolvida para uma infraestrutura pessoal: **não é uma solução pronta para instalação**. Arquivos de instalação e configuração são referências que precisam ser revisadas e adaptadas antes de qualquer uso.

A API WhatsMiau é um projeto independente; a integração foi desenvolvida com base na versão 1.5.0.

O painel usa Python 3.10 ou superior e JavaScript. Para visualizar a interface com dados simulados, execute `python dev_server.py` e abra `http://localhost:8765/painel/`. Essa demonstração não conecta ao WhatsApp.
