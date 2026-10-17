use framework "Foundation"
use scripting additions

on encodedText(theText)
    set sourceText to current application's NSString's stringWithString:(theText as text)
    set sourceData to sourceText's dataUsingEncoding:(current application's NSUTF8StringEncoding)
    return (sourceData's base64EncodedStringWithOptions:0) as text
end encodedText

on elementRole(theElement)
    tell application "System Events"
        return (value of attribute "AXRole" of theElement) as text
    end tell
end elementRole

on elementValue(theElement)
    tell application "System Events"
        set sourceValue to value of attribute "AXValue" of theElement
    end tell
    if sourceValue is missing value then error "STRUCTURE_CHANGED" number -2704
    return sourceValue as text
end elementValue

on observedLabel(titleText, helpText, descriptionText)
    if titleText is not missing value and titleText is not "" then return titleText as text
    if helpText is not missing value and helpText is not "" then return helpText as text
    if descriptionText is missing value or descriptionText is "" or descriptionText is "按钮" then return ""
    return descriptionText as text
end observedLabel

on attributeText(theElement, attributeName)
    set sourceValue to missing value
    tell application "System Events"
        try
            set sourceValue to value of attribute attributeName of theElement
        end try
    end tell
    if sourceValue is missing value then return ""
    return sourceValue as text
end attributeText

on elementLabel(theElement)
    set titleText to my attributeText(theElement, "AXTitle")
    set helpText to ""
    set descriptionText to ""
    if titleText is "" then set helpText to my attributeText(theElement, "AXHelp")
    if titleText is "" and helpText is "" then set descriptionText to my attributeText(theElement, "AXDescription")
    return my observedLabel(titleText, helpText, descriptionText)
end elementLabel

on elementX(theElement)
    tell application "System Events"
        set coordinates to value of attribute "AXPosition" of theElement
    end tell
    if (count coordinates) is not 2 then error "STRUCTURE_CHANGED" number -2704
    return item 1 of coordinates
end elementX

on allContents(theElement)
    tell application "System Events"
        return entire contents of theElement
    end tell
end allContents

on mainWindowIndex(windowNames)
    set matchingIndex to 0
    considering case
        repeat with windowIndex from 1 to count windowNames
            if item windowIndex of windowNames is "企业微信" then
                if matchingIndex is not 0 then error "STRUCTURE_CHANGED" number -2704
                set matchingIndex to windowIndex
            end if
        end repeat
    end considering
    if matchingIndex is 0 then error "NO_WINDOW" number -2701
    return matchingIndex
end mainWindowIndex

on validateWindowInventory(targetProcess, targetWindow, expectedWindowCount)
    tell application "System Events"
        set windowNames to name of every window of targetProcess
        set targetName to name of targetWindow
    end tell
    if (count windowNames) is not expectedWindowCount then error "STRUCTURE_CHANGED" number -2704
    my mainWindowIndex(windowNames)
    considering case
        if targetName is not "企业微信" then error "NO_WINDOW" number -2701
    end considering
end validateWindowInventory

on validateObservedTitle(directTitles, expectedTitle)
    considering case
        if expectedTitle is not "Ri@微信" then error "TITLE_MISMATCH" number -2702
        if (count directTitles) is 1 then
            if item 1 of directTitles is not "Ri@微信" then error "TITLE_MISMATCH" number -2702
        else if (count directTitles) is 2 then
            if item 1 of directTitles is not "Ri" or item 2 of directTitles is not "@微信" then error "TITLE_MISMATCH" number -2702
        else
            error "STRUCTURE_CHANGED" number -2704
        end if
    end considering
    return "Ri@微信"
end validateObservedTitle

on validateTitle(theWindow, expectedTitle)
    tell application "System Events"
        set targetPanel to splitter group 1 of splitter group 1 of theWindow
        set fieldCount to count text fields of targetPanel
        set paneCount to count splitter groups of targetPanel
        set directTitles to value of every static text of targetPanel
    end tell
    if fieldCount is not 1 or paneCount is not 1 then error "STRUCTURE_CHANGED" number -2704
    return my validateObservedTitle(directTitles, expectedTitle)
end validateTitle

on validateObservedPersonal(staticValues, textAreaValues, selectedCount)
    if selectedCount is not 1 or (count staticValues) is not 4 then error "NOT_PERSONAL" number -2703
    considering case
        if item 1 of staticValues is not "Ri" or item 4 of staticValues is not "@微信" then error "NOT_PERSONAL" number -2703
        if item 3 of staticValues is "" then error "NOT_PERSONAL" number -2703
        repeat with sourceValue in textAreaValues
            if (contents of sourceValue) is not "" then error "NOT_PERSONAL" number -2703
        end repeat
    end considering
    return true
end validateObservedPersonal

on validatePersonal(sidebarTable)
    tell application "System Events"
        set sidebarRows to rows of sidebarTable
    end tell
    set selectedCount to 0
    set staticValues to {}
    set textAreaValues to {}
    repeat with rowRef in sidebarRows
        set sidebarRow to contents of rowRef
        set rowSelected to false
        try
            tell application "System Events"
                set rowSelected to (value of attribute "AXSelected" of sidebarRow) as boolean
            end tell
        end try
        if rowSelected then
            set selectedCount to selectedCount + 1
            repeat with childRef in (my allContents(sidebarRow))
                set childElement to contents of childRef
                set childRole to my elementRole(childElement)
                if childRole is "AXStaticText" then
                    set end of staticValues to my elementValue(childElement)
                else if childRole is "AXTextArea" then
                    set end of textAreaValues to my elementValue(childElement)
                end if
            end repeat
        end if
    end repeat
    my validateObservedPersonal(staticValues, textAreaValues, selectedCount)
end validatePersonal

on validateObservedHistory(headingValues, fieldCount, scrollCount, buttonCount, closeLabel, queryValue, checkValues)
    if (count headingValues) is not 1 then error "HISTORY_CLOSED" number -2705
    if item 1 of headingValues is not "查找聊天记录" or closeLabel is not "关闭" then error "HISTORY_CLOSED" number -2705
    if fieldCount is not 1 or scrollCount is not 2 or buttonCount is not 3 then error "STRUCTURE_CHANGED" number -2704
    if queryValue is not "" then error "SEARCH_FILTERED" number -2706
    if (count checkValues) is not 5 then error "STRUCTURE_CHANGED" number -2704
    repeat with checkIndex from 1 to 5
        if checkIndex is 1 then
            if item checkIndex of checkValues is not 1 then error "SEARCH_FILTERED" number -2706
        else
            if item checkIndex of checkValues is not 0 then error "SEARCH_FILTERED" number -2706
        end if
    end repeat
    return true
end validateObservedHistory

on discoverHistory(historyPanel)
    tell application "System Events"
        if (count static texts of historyPanel) is not 1 or (count text fields of historyPanel) is not 1 or (count scroll areas of historyPanel) is not 2 or (count buttons of historyPanel) is not 3 then error "STRUCTURE_CHANGED" number -2704
        set headingElement to static text 1 of historyPanel
        set closeElement to button 1 of historyPanel
        set queryField to text field 1 of historyPanel
        set filterArea to scroll area 1 of historyPanel
        set messageArea to scroll area 2 of historyPanel
        set checkElements to checkboxes of filterArea
        if (count tables of messageArea) is not 1 then error "STRUCTURE_CHANGED" number -2704
        set historyTable to table 1 of messageArea
    end tell
    return {historyPanel, headingElement, closeElement, queryField, checkElements, historyTable}
end discoverHistory

on validateHistory(historyStructure, sidebarTable)
    set historyPanel to item 1 of historyStructure
    set headingElement to item 2 of historyStructure
    set closeElement to item 3 of historyStructure
    set queryField to item 4 of historyStructure
    set checkElements to item 5 of historyStructure
    set historyTable to item 6 of historyStructure
    tell application "System Events"
        set headingValues to value of every static text of historyPanel
        set fieldCount to count text fields of historyPanel
        set scrollCount to count scroll areas of historyPanel
        set buttonCount to count buttons of historyPanel
    end tell
    if (my elementRole(headingElement)) is not "AXStaticText" or (my elementRole(closeElement)) is not "AXButton" or (my elementRole(queryField)) is not "AXTextField" then error "STRUCTURE_CHANGED" number -2704
    if (my elementX(historyTable)) ≤ (my elementX(sidebarTable)) then error "STRUCTURE_CHANGED" number -2704
    set checkValues to {}
    repeat with checkRef in checkElements
        set checkElement to contents of checkRef
        if (my elementRole(checkElement)) is not "AXCheckBox" then error "STRUCTURE_CHANGED" number -2704
        tell application "System Events"
            set end of checkValues to value of attribute "AXValue" of checkElement
        end tell
    end repeat
    my validateObservedHistory(headingValues, fieldCount, scrollCount, buttonCount, my elementLabel(closeElement), my elementValue(queryField), checkValues)
end validateHistory

on snapshotText(expectedTitle)
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
    set initialWindowCount to count windowNames
    tell application "System Events"
        set targetWindow to window "企业微信" of targetProcess
    end tell
    my validateWindowInventory(targetProcess, targetWindow, initialWindowCount)
    set currentTitle to my validateTitle(targetWindow, expectedTitle)
    tell application "System Events"
        set targetPanel to splitter group 1 of splitter group 1 of targetWindow
        if (count scroll areas of targetPanel) is not 1 then error "STRUCTURE_CHANGED" number -2704
        set sidebarArea to scroll area 1 of targetPanel
        if (count tables of sidebarArea) is not 1 then error "STRUCTURE_CHANGED" number -2704
        set sidebarTable to table 1 of sidebarArea
        set historyPanel to splitter group 1 of targetPanel
    end tell
    my validatePersonal(sidebarTable)
    set historyStructure to my discoverHistory(historyPanel)
    set historyTable to item 6 of historyStructure
    my validateHistory(historyStructure, sidebarTable)
    tell application "System Events"
        set historyRows to rows of historyTable
    end tell
    set snapshotLines to {"WECOM_PERSONAL_HISTORY" & tab & "1", "TITLE" & tab & (my encodedText(currentTitle)), "PERSONAL" & tab & "1", "HISTORY" & tab & "1", "SEARCH" & tab}
    repeat with rowRef in historyRows
        set historyRow to contents of rowRef
        set senderLines to {}
        set bodyLines to {}
        set labelLines to {}
        repeat with elementRef in (my allContents(historyRow))
            set childElement to contents of elementRef
            set childRole to my elementRole(childElement)
            if childRole is "AXButton" then
                set end of senderLines to "SENDER" & tab & (my encodedText(my elementLabel(childElement)))
            else if childRole is "AXTextArea" then
                set end of bodyLines to "TEXT" & tab & (my encodedText(my elementValue(childElement)))
            else if childRole is "AXStaticText" then
                set end of labelLines to "LABEL" & tab & (my encodedText(my elementValue(childElement)))
            end if
        end repeat
        set end of snapshotLines to "ROW" & tab & ((count senderLines) as text) & tab & ((count bodyLines) as text) & tab & ((count labelLines) as text)
        set snapshotLines to snapshotLines & senderLines & bodyLines & labelLines & {"ENDROW"}
    end repeat
    my validateWindowInventory(targetProcess, targetWindow, initialWindowCount)
    my validateTitle(targetWindow, expectedTitle)
    my validatePersonal(sidebarTable)
    my validateHistory(historyStructure, sidebarTable)
    tell application "System Events"
        if (count rows of historyTable) is not (count historyRows) then error "STRUCTURE_CHANGED" number -2704
    end tell
    set end of snapshotLines to "END" & tab & ((count historyRows) as text)
    set previousDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to linefeed
    set resultText to snapshotLines as text
    set AppleScript's text item delimiters to previousDelimiters
    return resultText
end snapshotText

on run argv
    if (count argv) is not 1 then return "ERROR" & tab & "TITLE_MISMATCH"
    try
        return my snapshotText(item 1 of argv)
    on error errorText number errorNumber
        if errorNumber is -2701 or errorNumber is -1719 then return "ERROR" & tab & "NO_WINDOW"
        if errorNumber is -2702 then return "ERROR" & tab & "TITLE_MISMATCH"
        if errorNumber is -2703 then return "ERROR" & tab & "NOT_PERSONAL"
        if errorNumber is -2704 then return "ERROR" & tab & "STRUCTURE_CHANGED"
        if errorNumber is -2705 then return "ERROR" & tab & "HISTORY_CLOSED"
        if errorNumber is -2706 then return "ERROR" & tab & "SEARCH_FILTERED"
        if errorNumber is -1743 or errorNumber is -25211 then return "ERROR" & tab & "PERMISSION_DENIED"
        return "ERROR" & tab & "READ_FAILED"
    end try
end run
