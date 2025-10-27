# remove from current shell history (bash)
history -d $(history 1)   # only if the secret was the last command; otherwise remove the specific history line number
# safer: overwrite entire history file then reload
cat /dev/null > ~/.bash_history && history -c


grep -qxF 'source .venv/bin/activate' ~/.bashrc || echo 'source .venv/bin/activate' >> ~/.bashrc

IFS=$'\n\t'

LOG(){ printf '%s %s\n' "$(date --iso-8601=seconds)" "$*"; }
REQ_SUDO(){ if ! sudo -n true 2>/dev/null; then LOG "sudo required; you will be prompted."; fi }

PULUMI_VERSION="${PULUMI_VERSION:-3.196.0}"
HF_HUB_PIP_VERSION="${HF_HUB_PIP_VERSION:-0.34.4}"

# small helper to run commands as root when needed
ROOT_CMD(){ if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi }

install_prereqs(){
  LOG "Installing minimal prerequisite packages (curl, unzip, python3-pip)"
  ROOT_CMD apt-get update -y
  ROOT_CMD apt-get install -y --no-install-recommends curl unzip ca-certificates python3-pip || {
    LOG "apt-get install failed"
    exit 1
  }
}

install_aws_cli(){
  if command -v aws >/dev/null 2>&1; then
    LOG "aws CLI already installed -> $(aws --version 2>&1 | head -n1)"
    return
  fi
  LOG "Installing AWS CLI v2"
  TMP_ZIP="/tmp/awscliv2.zip"
  curl -fsSL --retry 3 -o "${TMP_ZIP}" "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"
  unzip -q "${TMP_ZIP}" -d /tmp
  ROOT_CMD /tmp/aws/install --update
  rm -rf /tmp/aws "${TMP_ZIP}"
  LOG "aws CLI installed -> $(aws --version 2>&1 | head -n1)"
}

install_pulumi(){
  if command -v pulumi >/dev/null 2>&1; then
    INSTALLED="$(pulumi version 2>/dev/null || true)"
    if [[ -n "${INSTALLED}" && "${INSTALLED}" == *"${PULUMI_VERSION}"* ]]; then
      LOG "pulumi ${PULUMI_VERSION} already installed -> ${INSTALLED}"
      return
    fi
  fi
  LOG "Installing pulumi v${PULUMI_VERSION}"
  URL="https://get.pulumi.com/releases/sdk/pulumi-v${PULUMI_VERSION}-linux-x64.tar.gz"
  TMP_TGZ="/tmp/pulumi.tgz"
  curl -fsSL --retry 3 -o "${TMP_TGZ}" "${URL}"
  # extract binaries into /usr/local/bin (requires sudo)
  mkdir -p /tmp/pulumi_extract
  tar -xzf "${TMP_TGZ}" -C /tmp/pulumi_extract
  # tar contains bin/; copy binaries
  ROOT_CMD cp -a /tmp/pulumi_extract/bin/* /usr/local/bin/
  rm -rf /tmp/pulumi_extract "${TMP_TGZ}"
  LOG "pulumi installed -> $(pulumi version 2>/dev/null || echo 'unknown')"
}

install_hf_hub(){
  LOG "Upgrading pip and installing huggingface_hub==${HF_HUB_PIP_VERSION}"
  python3 -m pip install --upgrade pip setuptools wheel >/dev/null
  python3 -m pip install "huggingface_hub==${HF_HUB_PIP_VERSION}"
  LOG "huggingface_hub installed -> $(python3 -c 'import huggingface_hub, sys; print(huggingface_hub.__version__)')"
}

main(){
  REQ_SUDO
  install_prereqs
  install_aws_cli
  install_pulumi
  install_hf_hub
  LOG "Done. Verify: aws, pulumi, python3 -c 'import huggingface_hub'"
}

main "$@"




if [ "${ENABLE_CROSS_ENCODER:-true}" = "true" ]; then
  echo "[info] ollama container (optional)"
  docker rm -f ollama 2>/dev/null || true
  docker run -d --name ollama -v ollama_models:/root/.ollama -p 11434:11434 -e OLLAMA_HOST=0.0.0.0 ollama/ollama:latest || true
fi
echo "[info] waiting a few seconds for qdrant/neo4j to become ready (adjust if needed)"
sleep 6
echo "[info] running ingestion (this will wait for embed handle if Serve not ready)"
python3 ./indexing_pipeline/ingest.py || (echo "[error] ingest.py failed" && exit 1)
echo "[info] ingestion finished"
echo "[info] you can now run inference pipeline or tests"
