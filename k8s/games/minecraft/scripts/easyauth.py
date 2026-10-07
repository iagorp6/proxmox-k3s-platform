"""Init: grava as mensagens do EasyAuth em português (config/EasyAuth/translation.conf).

O EasyAuth traduz as mensagens pelo idioma do cliente de cada jogador; aqui a tradução do lado
do servidor é desligada e todo mundo recebe o texto abaixo, com o passo a passo de /register e
/login. O arquivo é HOCON, que aceita JSON, e é sobrescrito a cada inicialização: para mudar um
texto, edite MESSAGES e reinicie o pod. Códigos de cor: §a verde, §c vermelho, §e amarelo,
§6 dourado, §f branco, §7 cinza, §l negrito, §r limpa a formatação. %s é preenchido pelo mod.
"""
import json
import pathlib
import re

MESSAGES = {
    # --- o que o jogador vê ao entrar (repetido a cada 10 s até autenticar) ---
    "registerRequired": (
        "§6§lBem-vindo ao AsunBoid!§r §eSeu personagem fica travado até você criar uma senha.\n"
        "§fAperte §aT§f, digite §a/register SUASENHA SUASENHA§f e aperte §aEnter§f.\n"
        "§7(a mesma senha duas vezes; guarde ela, você vai usar no /login)"
    ),
    "loginRequired": (
        "§6§lBem-vindo de volta!§r §eSeu personagem fica travado até você entrar na conta.\n"
        "§fAperte §aT§f, digite §a/login SUASENHA§f e aperte §aEnter§f.\n"
        "§7(esqueceu a senha? peça para um admin resetar no Discord)"
    ),
    "notAuthenticated": (
        "§cVocê ainda não entrou na sua conta.\n"
        "§fPrimeira vez: §a/register SUASENHA SUASENHA\n"
        "§fJá tem conta: §a/login SUASENHA"
    ),
    "registerRequiredWithGlobalPassword": (
        "§ePara criar sua conta você precisa da senha do servidor.\n"
        "§fDigite §a/register SENHADOSERVIDOR SUASENHA SUASENHA"
    ),
    # --- respostas do /register e do /login ---
    "registerSuccess": (
        "§aConta criada, pode jogar!\n"
        "§7Nas próximas vezes use §f/login SUASENHA§7. Para trocar a senha: §f/account changePassword ANTIGA NOVA"
    ),
    "successfullyAuthenticated": "§aLogin feito. Bom jogo!",
    "validSession": "§aO servidor lembrou de você, não precisa de /login. Bom jogo!",
    "onlinePlayerLogin": "§aConta original reconhecida, não precisa de /login. Bom jogo!",
    "alreadyAuthenticated": "§eVocê já está logado.",
    "alreadyRegistered": "§eEsse nick já tem conta. §fUse §a/login SUASENHA",
    "wrongPassword": "§cSenha incorreta. §fTente de novo: §a/login SUASENHA",
    "wrongGlobalPassword": "§cSenha do servidor incorreta.",
    "matchPassword": "§cAs duas senhas não são iguais. §fDigite a mesma senha duas vezes: §a/register SUASENHA SUASENHA",
    "enterPassword": "§eFaltou a senha. §fUse §a/login SUASENHA",
    "enterNewPassword": "§eFaltou a senha nova.",
    "minPasswordChars": "§cSenha curta demais. Use pelo menos %s caracteres.",
    "maxPasswordChars": "§cSenha longa demais. Use no máximo %s caracteres.",
    "passwordUpdated": "§aSenha alterada!",
    "cannotChangePassword": "§cVocê não pode trocar a senha.",
    "successfulLogout": "§aVocê saiu da conta.",
    "cannotLogout": "§cVocê não pode sair da conta.",
    "accountDeleted": "§aSua conta foi apagada.",
    "cannotUnregister": "§cVocê não pode apagar essa conta.",
    # --- motivos de expulsão ou bloqueio na entrada ---
    "timeExpired": (
        "§cO tempo para entrar na conta acabou.\n"
        "§fEntre de novo e digite §a/register SUASENHA SUASENHA§f (primeira vez) ou §a/login SUASENHA"
    ),
    "loginTriesExceeded": "§cMuitas senhas erradas. Espere alguns minutos e tente de novo.",
    "playerAlreadyOnline": "§cO jogador %s já está online.",
    "disallowedUsername": "§cNick inválido. Padrão permitido: %s",
    "differentUsernameCase": "§cSeu nick está com maiúsculas/minúsculas diferentes do cadastro. Use exatamente o nick original.",
    "corruptedPlayerData": "§cSeus dados podem estar corrompidos. Avise um admin no Discord.",
    "ipLimitExceeded": "§cMuitas contas no seu IP. Máximo: %s por IP.",
    "sessionLimitExceeded": "§cMuitas conexões do seu IP. Tente de novo daqui a pouco.",
    "uuidChanged": "§eUm admin alterou seu UUID. Entre de novo no servidor.",
    "databaseError": "§cErro no banco de dados do login. Avise um admin no Discord.",
    "unknownError": "§cErro desconhecido. Avise um admin no Discord.",
    # --- comandos de admin (/auth) e de conta (/account) ---
    "globalPasswordSet": "§aSenha do servidor definida.",
    "userdataDeleted": "§aDados do jogador apagados.",
    "userdataUpdated": "§aDados do jogador atualizados.",
    "userNotRegistered": "§cEsse jogador não tem conta.",
    "configurationReloaded": "§aConfiguração recarregada.",
    "worldSpawnSet": "§aPonto de espera do login definido.",
    "offlineUuid": "UUID offline do jogador %s (clique para copiar):\n%s",
    "registeredPlayers": "Jogadores com conta:",
    "markAsOffline": "§aJogador %s marcado como conta não original.",
    "markAsOnline": "§aJogador %s marcado como conta original.",
    "selfMarkAsOnline": "§aVocê marcou sua conta como original.",
    "selfMarkAsOnlineWarning": (
        "§eVocê vai marcar sua conta como original.\n"
        "§eSem uma conta original você não consegue mais entrar, e o que está ligado ao UUID atual "
        "(pets, descontos de aldeões) é perdido.\n"
        "§aPara confirmar: /account online SUASENHA true"
    ),
    "accountNotFound": "§cConta original não encontrada.",
    "accountCheckFailed": "§cServidor da Mojang indisponível. Tente mais tarde.",
    "ipLimitAdminNotify": "§e[EasyAuth] §cLimite por IP: §fjogador §e%s §fdo IP §e%s §fpassou do limite de §e%s §fcontas. Contas existentes: §e%s",
    "uuidSet": "§aUUID forçado do jogador §e%s §adefinido como §e%s§a. Ele será desconectado para aplicar.",
    "uuidCleared": "§aUUID forçado do jogador §e%s §aremovido. Vale a partir do próximo login.",
    "invalidUuid": "§cUUID inválido: §e%s§c. Formato: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "noForcedUuid": "§cO jogador §e%s §cnão tem UUID forçado.",
}


def kebab(name):
    return re.sub(r"([A-Z])", lambda m: "-" + m.group(1).lower(), name)


conf = {"enable-server-side-translation": False, "default-language": "pt_br"}
for key, text in MESSAGES.items():
    conf[kebab(key)] = {"text": text, "enabled": True, "serverSide": False}

path = pathlib.Path("/data/config/EasyAuth/translation.conf")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(conf, indent=2) + "\n", encoding="ascii")
print(f"translation.conf: {len(MESSAGES)} mensagens em português")
