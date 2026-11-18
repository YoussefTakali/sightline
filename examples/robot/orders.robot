*** Settings ***
Documentation     The demo app (docker/linux-target) driven only through pixels.
...               Run:  SIGHTLINE_VNC_PASSWORD=... robot --outputdir out examples/robot
Library           sightline.rf.SightlineLibrary    profile=headless    config=${CURDIR}/../sightline.toml
Library           Collections


*** Test Cases ***
Login
    Wait Until Visible    text:Connexion    timeout=30
    Type Text      Jean      into=text:Utilisateur : @right:200
    Type Secret    secret    into=text:Mot de passe : @right:200
    Click          text:Valider    expect=text:Bienvenue Jean

Find An Order By Scrolling And Open It
    Scroll Until Visible    exact:CMD-0042    area=text:Numéro @below:300
    Double Click            exact:CMD-0042
    Element Should Be Visible    text:Commande CMD-0042 - Martin
    Click                   exact:Fermer    vanish=True

Read The Whole Grid Exactly
    # [1]: the grid cell, not the same number in the status bar below it
    ${table}=    Read Table Via Clipboard    exact:CMD-0042[1]
    ...          columns=Numéro|Client|Montant|Statut    must_contain=CMD-0042
    Length Should Be    ${table}    61
    ${row}=      Get From List    ${table}    41
    Should Be Equal    ${row}[1]    Lefèvre

Export With Dialogs In Any Order
    FOR    ${i}    IN RANGE    2
        Click    exact:Exporter
        Handle Dialogs    required=exact:OK    optional=exact:Oui    quiet=1
    END
