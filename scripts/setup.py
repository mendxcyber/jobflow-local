from pathlib import Path
import shutil
root=Path(__file__).resolve().parents[1]
for name in ["candidato", "respostas"]:
    target=root/"config"/(name+".json")
    if not target.exists(): shutil.copyfile(root/"config"/(name+".example.json"),target)
for name in ["dados","curriculos"]: (root/name).mkdir(exist_ok=True)
print("Arquivos locais preparados. Edite config/candidato.json antes do uso real.")
