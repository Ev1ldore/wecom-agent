-- Fixed snapshot guard handlers are prepended by the packaged Python runner.
-- This action changes only the history pane's open/closed state.
property refreshActionStarted : false

on observedRefreshError(errorNumber, actionStarted)
    if errorNumber is -2701 or errorNumber is -1719 then return "NO_WINDOW"
    if errorNumber is -2702 then return "TITLE_MISMATCH"
    if errorNumber is -2703 then return "NOT_PERSONAL"
    if errorNumber is -2704 then return "STRUCTURE_CHANGED"
    if errorNumber is -2705 then return "HISTORY_CLOSED"
    if errorNumber is -2706 then return "SEARCH_FILTERED"
    if errorNumber is -2707 then return "REFRESH_INCOMPLETE"
    if errorNumber is -1743 or errorNumber is -25211 then return "PERMISSION_DENIED"
    if actionStarted is true then return "REFRESH_INCOMPLETE"
    return "REFRESH_FAILED"
end observedRefreshError

on refreshToggleState(helpValue, roleValue, enabledValue)
    if roleValue is not "AXButton" or enabledValue is not true then error "STRUCTURE_CHANGED" number -2704
    considering case
        if helpValue is "关闭聊天记录" then return "open"
        if helpValue is "查找聊天记录" then return "closed"
    end considering
    error "STRUCTURE_CHANGED" number -2704
end refreshToggleState

on observedRefreshPlan(stateValue, recoverClosed)
    if stateValue is "open" then return {"close", "open"}
    if stateValue is "closed" and recoverClosed is true then return {"open"}
    if stateValue is "closed" then error "HISTORY_CLOSED" number -2705
    error "STRUCTURE_CHANGED" number -2704
end observedRefreshPlan

on observedRefreshToggleIndex(helpValues)
    set matchingIndex to 0
    considering case
        repeat with buttonIndex from 1 to count helpValues
            set helpValue to item buttonIndex of helpValues
            if helpValue is "关闭聊天记录" or helpValue is "查找聊天记录" then
                if matchingIndex is not 0 then error "STRUCTURE_CHANGED" number -2704
                set matchingIndex to buttonIndex
            end if
        end repeat
    end considering
    if matchingIndex is 0 then error "STRUCTURE_CHANGED" number -2704
    return matchingIndex
end observedRefreshToggleIndex

on discoverRefreshToggle(targetWindow)
    tell application "System Events"
        set toolbarParent to splitter group 1 of splitter group 1 of splitter group 1 of splitter group 1 of targetWindow
        set toolbarButtons to buttons of toolbarParent
    end tell
    if (my elementRole(toolbarParent)) is not "AXSplitGroup" then error "STRUCTURE_CHANGED" number -2704
    set helpValues to {}
    repeat with buttonRef in toolbarButtons
        set end of helpValues to my attributeText(contents of buttonRef, "AXHelp")
    end repeat
    return item (my observedRefreshToggleIndex(helpValues)) of toolbarButtons
end discoverRefreshToggle

on currentRefreshState(toggleElement)
    tell application "System Events"
        set enabledValue to value of attribute "AXEnabled" of toggleElement
    end tell
    return my refreshToggleState(my attributeText(toggleElement, "AXHelp"), my elementRole(toggleElement), enabledValue)
end currentRefreshState

on validateRefreshIdentity(targetProcess, targetWindow, windowCount, sidebarTable)
    my validateWindowInventory(targetProcess, targetWindow, windowCount)
    my validateTitle(targetWindow, "Ri@微信")
    my validatePersonal(sidebarTable)
end validateRefreshIdentity

on waitForRefreshState(targetWindow, expectedState)
    repeat 20 times
        if (my currentRefreshState(my discoverRefreshToggle(targetWindow))) is expectedState then return
        delay 0.05
    end repeat
    error "REFRESH_INCOMPLETE" number -2707
end waitForRefreshState

on pressRefreshToggle(expectedState, targetProcess, targetWindow, windowCount, sidebarTable)
    my validateRefreshIdentity(targetProcess, targetWindow, windowCount, sidebarTable)
    if expectedState is "open" then
        tell application "System Events"
            set targetPanel to splitter group 1 of splitter group 1 of targetWindow
            set historyPanel to splitter group 1 of targetPanel
        end tell
        my validateHistory(my discoverHistory(historyPanel), sidebarTable)
    end if
    set toggleElement to my discoverRefreshToggle(targetWindow)
    if (my currentRefreshState(toggleElement)) is not expectedState then error "STRUCTURE_CHANGED" number -2704
    set my refreshActionStarted to true
    tell application "System Events"
        perform action "AXPress" of toggleElement
    end tell
end pressRefreshToggle

on refreshHistory(expectedTitle, recoverClosed)
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
    my validateRefreshIdentity(targetProcess, targetWindow, windowCount, sidebarTable)
    set initialState to my currentRefreshState(my discoverRefreshToggle(targetWindow))
    set actionPlan to my observedRefreshPlan(initialState, recoverClosed)
    if initialState is "open" then
        tell application "System Events"
            set historyPanel to splitter group 1 of targetPanel
        end tell
        my validateHistory(my discoverHistory(historyPanel), sidebarTable)
    end if
    repeat with actionRef in actionPlan
        set actionName to contents of actionRef
        if actionName is "close" then
            my pressRefreshToggle("open", targetProcess, targetWindow, windowCount, sidebarTable)
            my waitForRefreshState(targetWindow, "closed")
        else if actionName is "open" then
            my pressRefreshToggle("closed", targetProcess, targetWindow, windowCount, sidebarTable)
            my waitForRefreshState(targetWindow, "open")
        else
            error "STRUCTURE_CHANGED" number -2704
        end if
    end repeat
    my validateRefreshIdentity(targetProcess, targetWindow, windowCount, sidebarTable)
    tell application "System Events"
        set historyPanel to splitter group 1 of targetPanel
    end tell
    my validateHistory(my discoverHistory(historyPanel), sidebarTable)
    return "OK"
end refreshHistory

on run argv
    set my refreshActionStarted to false
    if (count argv) is not 2 then return "ERROR" & tab & "TITLE_MISMATCH"
    considering case
        if item 1 of argv is not "Ri@微信" then return "ERROR" & tab & "TITLE_MISMATCH"
        if item 2 of argv is not "refresh" and item 2 of argv is not "recover" then return "ERROR" & tab & "STRUCTURE_CHANGED"
    end considering
    try
        return my refreshHistory(item 1 of argv, item 2 of argv is "recover")
    on error errorText number errorNumber
        return "ERROR" & tab & my observedRefreshError(errorNumber, my refreshActionStarted)
    end try
end run
