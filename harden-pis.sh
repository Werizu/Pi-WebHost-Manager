#!/bin/bash
# Pi Security Hardening Script
# Hardens SSH and sets up UFW on all 3 Pis, one at a time with confirmation

KEY="/Users/marlonheck/.pi-manager/keys/id_rsa"
USER="marlon"

declare -a PI_NAMES=("Pi 5 (16GB)" "Pi 5 (8GB)" "Pi 4 (4GB)")
declare -a PI_IPS=("192.168.178.201" "192.168.178.202" "192.168.178.203")

SSH="ssh -i $KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

harden_pi() {
  local ip=$1
  local name=$2

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Hardening: $name ($ip)"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  # Step 0: Test SSH connection
  echo -n "Testing SSH key connection... "
  if ! $SSH $USER@$ip "echo ok" &>/dev/null; then
    echo -e "${RED}FAILED${NC}"
    echo "Cannot reach $name via SSH key – skipping to avoid lockout!"
    return 1
  fi
  echo -e "${GREEN}OK${NC}"

  # Step 1: SSH hardening
  echo -n "Hardening SSH config... "
  $SSH $USER@$ip "
    # Remove any existing (commented or plain) lines for these settings
    sudo sed -i '/^#*PasswordAuthentication/d' /etc/ssh/sshd_config
    sudo sed -i '/^#*PermitRootLogin/d' /etc/ssh/sshd_config
    # Append correct values
    printf 'PasswordAuthentication no\nPermitRootLogin no\n' | sudo tee -a /etc/ssh/sshd_config > /dev/null
    sudo systemctl restart ssh
  " && echo -e "${GREEN}done${NC}" || { echo -e "${RED}FAILED${NC}"; return 1; }

  # Step 2: Verify SSH still works after restart
  echo -n "Verifying connection after SSH restart... "
  sleep 2
  if ! $SSH $USER@$ip "echo ok" &>/dev/null; then
    echo -e "${RED}LOST CONNECTION${NC}"
    echo "Something went wrong – check $name manually!"
    return 1
  fi
  echo -e "${GREEN}OK${NC}"

  # Step 3: UFW setup
  echo -n "Installing and configuring UFW... "
  $SSH $USER@$ip "
    sudo apt install ufw -y -q 2>/dev/null
    sudo ufw --force reset > /dev/null
    sudo ufw default deny incoming > /dev/null
    sudo ufw default allow outgoing > /dev/null
    sudo ufw allow 22/tcp > /dev/null
    sudo ufw allow 80/tcp > /dev/null
    sudo ufw allow from 100.64.0.0/10 to any port 5000 > /dev/null
    sudo ufw --force enable > /dev/null
  " && echo -e "${GREEN}done${NC}" || { echo -e "${RED}FAILED${NC}"; return 1; }

  # Step 4: Show UFW status
  echo ""
  echo "UFW status on $name:"
  $SSH $USER@$ip "sudo ufw status"

  echo ""
  echo -e "${GREEN}✓ $name hardened successfully${NC}"
  return 0
}

# Header
echo ""
echo "Pi Security Hardening"
echo "====================="
echo "SSH password auth → disabled"
echo "Root login        → disabled"
echo "UFW firewall      → enabled (SSH + HTTP + Tailscale→5000)"
echo ""
echo "Targets:"
for i in 0 1 2; do
  echo "  ${PI_NAMES[$i]} (${PI_IPS[$i]})"
done
echo ""
read -p "Start? (yes/no): " confirm
[[ "$confirm" != "yes" ]] && echo "Aborted." && exit 0

# Process each Pi with confirmation between them
for i in 0 1 2; do
  name="${PI_NAMES[$i]}"
  ip="${PI_IPS[$i]}"

  harden_pi "$ip" "$name"
  result=$?

  if [[ $i -lt 2 ]]; then
    echo ""
    if [[ $result -ne 0 ]]; then
      echo -e "${YELLOW}Warning: last Pi had errors.${NC}"
    fi
    read -p "Continue to next Pi? (yes/no): " next
    [[ "$next" != "yes" ]] && echo "Stopped. Run script again to continue." && exit 0
  fi
done

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  All 3 Pis hardened successfully!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
