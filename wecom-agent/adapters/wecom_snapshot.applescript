use framework "Foundation"
use scripting additions

on encodedText(theText)
    set sourceText to current application's NSString's stringWithString:(theText as text)
    set sourceData to sourceText's dataUsingEncoding:(current application's NSUTF8StringEncoding)
    return (sourceData's base64EncodedStringWithOptions:0) as text
end encodedText

on numberText(theNumber)
    set sourceNumber to current application's NSNumber's numberWithDouble:(theNumber as real)
    return (sourceNumber's stringValue()) as text
end numberText

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

on elementRect(theElement)
    tell application "System Events"
        set coordinates to value of attribute "AXPosition" of theElement
        set dimensions to value of attribute "AXSize" of theElement
    end tell
    if (count coordinates) is not 2 or (count dimensions) is not 2 then error "STRUCTURE_CHANGED" number -2704
    return {item 1 of coordinates, item 2 of coordinates, item 1 of dimensions, item 2 of dimensions}
end elementRect

on rectRecord(theRect)
    return (my numberText(item 1 of theRect)) & tab & (my numberText(item 2 of theRect)) & tab & (my numberText(item 3 of theRect)) & tab & (my numberText(item 4 of theRect))
end rectRecord

on allContents(theElement)
    tell application "System Events"
        return entire contents of theElement
    end tell
end allContents

on protocolHeader(expectedTitle, messageRect)
    set snapshotLines to {"WECOM_DESKTOP_SNAPSHOT" & tab & "1", "TITLE" & tab & (my encodedText(expectedTitle)), "BOT" & tab & "1", "TABLE" & tab & (my rectRecord(messageRect))}
    return snapshotLines
end protocolHeader

on validateTitle(theWindow, expectedTitle)
    -- This layout was observed on the target Mac. Fail closed on layout changes.
    tell application "System Events"
        set targetPanel to splitter group 1 of splitter group 1 of theWindow
        set fieldCount to count text fields of targetPanel
        set paneCount to count splitter groups of targetPanel
        set directTitles to value of every static text of targetPanel
    end tell
    if fieldCount is not 1 or paneCount is not 1 then error "STRUCTURE_CHANGED" number -2704
    if (count directTitles) is not 1 then error "STRUCTURE_CHANGED" number -2704
    if (item 1 of directTitles) is not expectedTitle then error "TITLE_MISMATCH" number -2702
end validateTitle

on hasSelectedBot(sidebarTable)
    tell application "System Events"
        set sidebarRows to rows of sidebarTable
    end tell
    set selectedCount to 0
    set botCount to 0
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
                if (my elementRole(childElement)) is "AXTextArea" then
                    set sourceText to current application's NSString's stringWithString:(my elementValue(childElement))
                    set cleanText to (sourceText's stringByReplacingOccurrencesOfString:(character id 8203) withString:"") as text
                    if cleanText is "BOT" then set botCount to botCount + 1
                end if
            end repeat
        end if
    end repeat
    return selectedCount is 1 and botCount is 1
end hasSelectedBot

on snapshotText(expectedTitle)
    tell application "System Events"
        set targetProcesses to application processes whose bundle identifier is "com.tencent.WeWorkMac"
        if (count targetProcesses) is not 1 then error "NO_WINDOW" number -2701
        set targetProcess to item 1 of targetProcesses
        if (count windows of targetProcess) is 0 then error "NO_WINDOW" number -2701
        set targetWindow to window 1 of targetProcess
    end tell
    my validateTitle(targetWindow, expectedTitle)
    set tableElements to {}
    repeat with elementRef in (my allContents(targetWindow))
        set candidateElement to contents of elementRef
        if (my elementRole(candidateElement)) is "AXTable" then set end of tableElements to candidateElement
    end repeat
    if (count tableElements) is not 2 then error "STRUCTURE_CHANGED" number -2704
    set firstTable to item 1 of tableElements
    set secondTable to item 2 of tableElements
    set firstRect to my elementRect(firstTable)
    set secondRect to my elementRect(secondTable)
    if (item 1 of firstRect) is (item 1 of secondRect) then error "STRUCTURE_CHANGED" number -2704
    if (item 1 of firstRect) < (item 1 of secondRect) then
        set sidebarTable to firstTable
        set messageTable to secondTable
        set messageRect to secondRect
    else
        set sidebarTable to secondTable
        set messageTable to firstTable
        set messageRect to firstRect
    end if
    if not (my hasSelectedBot(sidebarTable)) then error "NOT_BOT" number -2703
    tell application "System Events"
        set messageRows to rows of messageTable
    end tell
    set snapshotLines to my protocolHeader(expectedTitle, messageRect)
    repeat with rowRef in messageRows
        set messageRow to contents of rowRef
        set textRecords to {}
        set timeRecords to {}
        repeat with elementRef in (my allContents(messageRow))
            set childElement to contents of elementRef
            set childRole to my elementRole(childElement)
            if childRole is "AXTextArea" then
                set end of textRecords to "TEXT" & tab & (my encodedText(my elementValue(childElement))) & tab & (my rectRecord(my elementRect(childElement)))
            else if childRole is "AXStaticText" then
                set end of timeRecords to "TIME" & tab & (my encodedText(my elementValue(childElement)))
            end if
        end repeat
        set end of snapshotLines to "ROW" & tab & ((count textRecords) as text) & tab & ((count timeRecords) as text)
        set snapshotLines to snapshotLines & textRecords & timeRecords & {"ENDROW"}
    end repeat
    my validateTitle(targetWindow, expectedTitle)
    if not (my hasSelectedBot(sidebarTable)) then error "NOT_BOT" number -2703
    set end of snapshotLines to "END" & tab & ((count messageRows) as text)
    set previousDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to linefeed
    set resultText to snapshotLines as text
    set AppleScript's text item delimiters to previousDelimiters
    return resultText
end snapshotText

on run argv
    if (count argv) is not 1 then return "ERROR" & tab & "TARGET_REQUIRED"
    try
        return my snapshotText(item 1 of argv)
    on error errorText number errorNumber
        if errorNumber is -2701 or errorNumber is -1719 then return "ERROR" & tab & "NO_WINDOW"
        if errorNumber is -2702 then return "ERROR" & tab & "TITLE_MISMATCH"
        if errorNumber is -2703 then return "ERROR" & tab & "NOT_BOT"
        if errorNumber is -2704 then return "ERROR" & tab & "STRUCTURE_CHANGED"
        if errorNumber is -1743 or errorNumber is -25211 then return "ERROR" & tab & "PERMISSION_DENIED"
        return "ERROR" & tab & "READ_FAILED"
    end try
end run
