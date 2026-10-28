# Apresentando o JobFlow Local

Organizar candidaturas envolve mais do que encontrar links. Depois da busca vêm a triagem, a preparação dos documentos e o acompanhamento do que já foi feito.

O JobFlow Local reúne essas etapas em um painel com filtros configuráveis, histórico em SQLite, geração de PDF e integração com n8n. A implementação usa Python e FastAPI, com execução local em Docker.

Esta primeira edição pública disponibiliza o código e exemplos fictícios para estudo e adaptação. O objetivo é mostrar como coleta, regras de negócio e estados de uma aplicação se conectam. Os coletores são heurísticos e exigem manutenção; o projeto não promete cobrir todos os sites ou enviar toda candidatura sem intervenção.

O desenvolvimento nasceu de uma necessidade pessoal e contou com apoio de IA na implementação e documentação. As próximas melhorias incluem modularizar os coletores, melhorar a validação de configurações e ampliar os testes.

Para experimentar, siga a instalação no README. Sugestões e relatos reproduzíveis são bem-vindos nas issues.
