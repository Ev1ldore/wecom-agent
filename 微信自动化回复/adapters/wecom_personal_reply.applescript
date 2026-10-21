-- Fixed read-only snapshot guard handlers are prepended by the Python runner.

on validateObservedComposer(roleValue, settableValue, enabledValue, sourceValue)
    if roleValue is not "AXTextArea" or settableValue is not true then error "COMPOSER_UNAVAILABLE" number -2711
    -- Qt may omit AXEnabled. Unknown is acceptable only for this read-only preflight.
    if enabledValue is not missing value and enabledValue is not true then error "COMPOSER_UNAVAILABLE" number -2711
    if sourceValue is not "" then error "COMPOSER_NOT_EMPTY" number -2713
    return true
end validateObservedComposer

on validateObservedComposerCount(editableCount)
    if editableCount is 0 then error "COMPOSER_UNAVAILABLE" number -2711
    if editableCount is not 1 then error "COMPOSER_AMBIGUOUS" number -2712
    return true
end validateObservedComposerCount

on validateObservedComposerLayout(panelRole, scrollCount, areaRole, textAreaCount)
    if panelRole is not "AXSplitGroup" or scrollCount is not 2 or areaRole is not "AXScrollArea" then error "STRUCTURE_CHANGED" number -2704
    my validateObservedComposerCount(textAreaCount)
    return true
end validateObservedComposerLayout

on composerSettable(composerElement)
    tell application "System Events"
        return settable of attribute "AXValue" of composerElement
    end tell
end composerSettable

on validateComposer(composerElement, sidebarTable)
    set enabledValue to missing value
    tell application "System Events"
        try
            set enabledValue to value of attribute "AXEnabled" of composerElement
        on error errorText number errorNumber
            if errorNumber is not -1728 then error "PREFLIGHT_FAILED" number errorNumber
        end try
    end tell
    my validateObservedComposer(my elementRole(composerElement), my composerSettable(composerElement), enabledValue, my elementValue(composerElement))
    if (my elementX(composerElement)) ≤ (my elementX(sidebarTable)) then error "STRUCTURE_CHANGED" number -2704
end validateComposer

on discoverComposer(targetWindow)
    tell application "System Events"
        set chatPanel to splitter group 1 of splitter group 1 of splitter group 1 of splitter group 1 of targetWindow
        set scrollCount to count scroll areas of chatPanel
        if scrollCount is not 2 then error "STRUCTURE_CHANGED" number -2704
        set composerArea to scroll area 2 of chatPanel
        set textAreaCount to count text areas of composerArea
    end tell
    my validateObservedComposerLayout(my elementRole(chatPanel), scrollCount, my elementRole(composerArea), textAreaCount)
    tell application "System Events"
        return text area 1 of composerArea
    end tell
end discoverComposer

on validateReplyIdentity(targetProcess, targetWindow, windowCount, sidebarTable)
    if (my elementRole(targetWindow)) is not "AXWindow" then error "STRUCTURE_CHANGED" number -2704
    my validateWindowInventory(targetProcess, targetWindow, windowCount)
    my validateTitle(targetWindow, "Ri@微信")
    my validatePersonal(sidebarTable)
end validateReplyIdentity

on preflightReply(expectedTitle)
    considering case
        if expectedTitle is not "Ri@微信" then error "TITLE_MISMATCH" number -2702
    end considering
    tell application "System Events"
        set targetProcesses to application processes whose bundle identifier is "com.tencent.WeWorkMac"
        if (count targetProcesses) is not 1 then error "NO_WINDOW" number -2701
        set targetProcess to item 1 of targetProcesses
        set windowNames to name of every window of targetProcess
    end tell
    my mainWindowIndex(windowNames)
    set windowCount to count windowNames
    tell application "System Events"
        set targetWindow to window "企业微信" of targetProcess
        set targetPanel to splitter group 1 of splitter group 1 of targetWindow
        if (count scroll areas of targetPanel) is not 1 then error "STRUCTURE_CHANGED" number -2704
        set sidebarArea to scroll area 1 of targetPanel
        if (count tables of sidebarArea) is not 1 then error "STRUCTURE_CHANGED" number -2704
        set sidebarTable to table 1 of sidebarArea
    end tell
    my validateReplyIdentity(targetProcess, targetWindow, windowCount, sidebarTable)
    set composerElement to my discoverComposer(targetWindow)
    my validateComposer(composerElement, sidebarTable)
    my validateReplyIdentity(targetProcess, targetWindow, windowCount, sidebarTable)
    set finalComposer to my discoverComposer(targetWindow)
    if finalComposer is not composerElement then error "COMPOSER_CHANGED" number -2714
    my validateComposer(finalComposer, sidebarTable)
    my validateReplyIdentity(targetProcess, targetWindow, windowCount, sidebarTable)
    return "OK"
end preflightReply

on run argv
    if (count argv) is not 1 then return "ERROR" & tab & "TITLE_MISMATCH"
    try
        return my preflightReply(item 1 of argv)
    on error errorText number errorNumber
        if errorNumber is -2701 or errorNumber is -1719 then return "ERROR" & tab & "NO_WINDOW"
        if errorNumber is -2702 then return "ERROR" & tab & "TITLE_MISMATCH"
        if errorNumber is -2703 then return "ERROR" & tab & "NOT_PERSONAL"
        if errorNumber is -2704 then return "ERROR" & tab & "STRUCTURE_CHANGED"
        if errorNumber is -2711 then return "ERROR" & tab & "COMPOSER_UNAVAILABLE"
        if errorNumber is -2712 then return "ERROR" & tab & "COMPOSER_AMBIGUOUS"
        if errorNumber is -2713 then return "ERROR" & tab & "COMPOSER_NOT_EMPTY"
        if errorNumber is -2714 then return "ERROR" & tab & "COMPOSER_CHANGED"
        if errorNumber is -1743 or errorNumber is -25211 then return "ERROR" & tab & "PERMISSION_DENIED"
        return "ERROR" & tab & "PREFLIGHT_FAILED"
    end try
end run
