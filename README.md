# burn-hardware

CLI em Python para gerar carga controlada em **CPU, memória, disco e GPU NVIDIA** no Linux. Você escolhe os recursos, a intensidade aproximada e a duração. Durante a execução, o programa mostra a utilização geral da CPU e, quando disponíveis, as temperaturas da CPU e da GPU.

O projeto serve para observar aquecimento, refrigeração e comportamento do computador sob carga. **Ele não valida a correção dos resultados dos cálculos nem substitui um teste de diagnóstico de memória ou disco.** Também não encerra o teste automaticamente ao atingir uma temperatura limite.

## Requisitos

- Linux e Python 3.10 ou superior.
- CPU e memória: nenhuma dependência Python adicional.
- Disco: espaço livre no volume escolhido; o programa exige uma reserva inicial de 2 GiB além do tamanho solicitado para o arquivo de teste.
- GPU: placa NVIDIA com CUDA funcional e pelo menos um backend instalado: `gpu-burn`, PyTorch com CUDA ou CuPy com CUDA.
- Monitoramento opcional da GPU: `nvidia-smi` disponível no `PATH`.

O programa usa interfaces específicas do Linux, como `/proc`, `/sys` e sinais POSIX. Ele não oferece suporte nativo a Windows ou macOS. A GPU de fabricantes diferentes da NVIDIA não é testada por este CLI.

## Começando

Na pasta do projeto, confira as opções:

```bash
python3 hardware_stress.py --help
```

Faça um teste curto com CPU e memória, que são os recursos padrão:

```bash
python3 hardware_stress.py --percent 50 --duration 60
```

`--percent` é obrigatório e aceita inteiros de 1 a 100. A duração padrão é 60 segundos. Pressione `Ctrl-C` para interromper; o programa encerra os processos de teste e remove o arquivo temporário de disco em uma parada normal.

## Exemplos por recurso

Selecione recursos com `--resources`, separados por vírgula. Os nomes válidos são `cpu`, `memory`, `disk` e `gpu`.

### CPU

```bash
python3 hardware_stress.py --percent 70 --resources cpu --duration 300
python3 hardware_stress.py --percent 70 --resources cpu --workers 4 --duration 300
```

O programa cria, por padrão, um processo por CPU lógica disponível. `--workers` permite escolher uma quantidade entre 1 e o número de CPUs lógicas disponíveis. Cada processo alterna períodos de trabalho e descanso para aproximar a intensidade solicitada; o uso observado depende também de outras cargas, do escalonador e dos limites de energia e temperatura do equipamento.

### Memória

```bash
python3 hardware_stress.py --percent 80 --resources memory --duration 120
python3 hardware_stress.py --percent 80 --resources memory --memory-reserve-mib 2048 --duration 120
```

Aqui, `--percent` representa a **ocupação total de RAM desejada**, incluindo a memória já em uso. O programa aloca apenas a diferença necessária e preserva, por padrão, 1 GiB disponível para o sistema. Quando detectável, também considera o limite de memória do contêiner. Se o uso atual já exceder a meta, não aloca mais memória. A quantidade efetivamente disponível pode mudar durante o teste.

### Disco

```bash
python3 hardware_stress.py --percent 60 --resources disk --duration 120 --disk-size-mib 1024
python3 hardware_stress.py --percent 60 --resources disk --disk-dir /caminho/para/volume --disk-size-mib 2048 --duration 120
```

O teste de disco é opcional e **faz gravações reais** em um arquivo temporário. `--disk-size-mib` define o tamanho máximo desse arquivo; o padrão é 1024 MiB. `--disk-dir` escolhe o volume a testar e, por padrão, usa o diretório temporário do sistema. A intensidade controla o tempo de escrita em cada ciclo, não uma taxa fixa de MB/s. A verificação de 2 GiB livres acontece antes de iniciar; outras atividades ainda podem consumir espaço durante o teste.

Em uma parada normal ou com `Ctrl-C`, o arquivo é removido. Se o processo for encerrado à força (`SIGKILL`, desligamento ou travamento), pode restar um arquivo `hardware-stress-*.bin` no diretório escolhido. Confira a origem antes de removê-lo.

### GPU NVIDIA

```bash
python3 hardware_stress.py --percent 80 --resources gpu --duration 120
python3 hardware_stress.py --percent 80 --resources gpu --gpu-backend torch --duration 120
```

No modo `auto`, o programa procura `gpu-burn` no `PATH`; depois tenta PyTorch com CUDA e, por último, CuPy com CUDA. Use `--gpu-backend gpu-burn`, `torch` ou `cupy` para escolher explicitamente. A intensidade é aproximada por períodos ativos e ociosos. O teste usa a primeira GPU CUDA disponível. Sem backend funcional, o programa encerra com erro e informa que a GPU não foi testada.

Para configurar PyTorch com CUDA, use o comando de instalação indicado para seu sistema e driver na [documentação oficial do PyTorch](https://pytorch.org/get-started/locally/). O [`gpu-burn`](https://github.com/wilicc/gpu-burn) também pode ser compilado e colocado no `PATH`. Antes de executar, confira se o driver reconhece a placa:

```bash
nvidia-smi
```

O monitoramento via `nvidia-smi` é independente do backend de carga: ter o comando disponível não basta para iniciar o teste de GPU.

## Testes combinados e execução contínua

```bash
# CPU e memória durante cinco minutos
python3 hardware_stress.py --percent 70 --resources cpu,memory --duration 300

# Todos os recursos, incluindo gravação em disco e GPU CUDA
python3 hardware_stress.py --percent 70 --all --duration 120

# Execução contínua até Ctrl-C
python3 hardware_stress.py --percent 60 --resources cpu,memory --forever
```

`--duration 0` também executa continuamente. `--all` exige um backend de GPU funcional; para testar apenas CPU, memória e disco, use `--resources cpu,memory,disk`.

## Como ler a saída

```text
RAM alvo: 8.00 GiB (reserva: 1024 MiB)
Estresse 70% em cpu, memory; Ctrl-C para encerrar.
[     5.0s] CPU host 72% | CPU 68.5°C | GPU 42.0°C/0%
```

`RAM alvo` é a quantidade adicional calculada para alocação. `CPU host` é a utilização geral do computador, não apenas deste programa; em contêineres, pode incluir cargas externas. A temperatura da GPU pode aparecer mesmo quando a GPU não está entre os recursos testados. Sensores ausentes ou inacessíveis simplesmente deixam de aparecer no relatório. Use `--report-interval` para mudar a frequência das linhas.

## Opções

| Opção | Padrão | Função |
| --- | --- | --- |
| `-p`, `--percent` | obrigatório | Intensidade de 1 a 100 |
| `--duration` | `60` | Duração em segundos; `0` executa continuamente |
| `--forever` | desativado | Executa continuamente até interrupção |
| `--resources` | `cpu,memory` | Recursos separados por vírgula |
| `--all` | desativado | Seleciona CPU, memória, disco e GPU |
| `--workers` | CPUs disponíveis | Quantidade de processos de CPU |
| `--memory-reserve-mib` | `1024` | RAM disponível a preservar |
| `--disk-size-mib` | `1024` | Tamanho máximo do arquivo de disco, em MiB |
| `--disk-dir` | diretório temporário do sistema | Diretório do arquivo de disco |
| `--gpu-backend` | `auto` | `auto`, `gpu-burn`, `torch` ou `cupy` |
| `--report-interval` | `5` | Intervalo do relatório em segundos, maior que zero |

## Cuidados e limitações

- Acompanhe temperaturas, ventilação e estabilidade, principalmente em testes longos ou contínuos. O programa **não possui corte térmico automático**.
- O teste de disco grava repetidamente e pode aumentar o desgaste do SSD. Escolha o volume e a duração conscientemente.
- A porcentagem não tem a mesma interpretação em todos os recursos: CPU, disco e GPU usam ciclos ativos e ociosos; memória usa uma meta de ocupação total.
- O programa gera carga e observa sinais básicos do sistema. Ele não verifica integridade de dados de disco ou memória e não produz laudo de estabilidade.
- Em contêineres, a leitura de limites de memória depende da disponibilidade dos arquivos de cgroup. A reserva de RAM é uma estimativa, não uma garantia contra falta de memória se outras aplicações alocarem ao mesmo tempo.
- Um encerramento forçado pode impedir a limpeza do processo externo `gpu-burn`. Se isso ocorrer, verifique se ele ainda está em execução antes de iniciar outro teste.

## Licença

Distribuído sob a [licença MIT](LICENSE).
