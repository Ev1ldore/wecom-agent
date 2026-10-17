-- Snapshot/preflight guards and a base64 data property are supplied on stdin.
property nativeWriteStarted : false

on decodedReplyText()
    set replyData to current application's NSData's alloc()'s initWithBase64EncodedString:encodedReply options:0
    if replyData is missing value then error "EMPTY_CONTENT" number -2720
    set replyString to current application's NSString's alloc()'s initWithData:replyData encoding:(current application's NSUTF8StringEncoding)
    if replyString is missing value then error "EMPTY_CONTENT" number -2720
    set replyText to replyString as text
    if replyText is "" then error "EMPTY_CONTENT" number -2720
    return replyText
end decodedReplyText

on sendElementRect(theElement)
    tell application "System Events"
        set coordinates to value of attribute "AXPosition" of theElement
        set dimensions to value of attribute "AXSize" of theElement
    end tell
    if (count coordinates) is not 2 or (count dimensions) is not 2 then error "STRUCTURE_CHANGED" number -2704
    return {item 1 of coordinates, item 2 of coordinates, item 1 of dimensions, item 2 of dimensions}
end sendElementRect

on discoverSendTable(targetWindow)
    tell application "System Events"
        set chatPanel to splitter group 1 of splitter group 1 of splitter group 1 of splitter group 1 of targetWindow
        if (count scroll areas of chatPanel) is not 2 then error "STRUCTURE_CHANGED" number -2704
        set messageArea to scroll area 1 of chatPanel
        if (count tables of messageArea) is not 1 then error "STRUCTURE_CHANGED" number -2704
        set messageTable to table 1 of messageArea
    end tell
    if (my elementRole(chatPanel)) is not "AXSplitGroup" or (my elementRole(messageArea)) is not "AXScrollArea" or (my elementRole(messageTable)) is not "AXTable" then error "STRUCTURE_CHANGED" number -2704
    return messageTable
end discoverSendTable

on requireSendFocus(targetProcess, composerElement)
    tell application "System Events"
        set processFrontmost to frontmost of targetProcess
        set composerFocused to value of attribute "AXFocused" of composerElement
    end tell
    if processFrontmost is not true or composerFocused is not true then error "FOCUS_FAILED" number -2721
end requireSendFocus

on observedOutgoingReceipt(beforeCount, afterCount, bodyCount, bodyText, expectedText, tableX, tableWidth, bodyX, bodyWidth, composerEmpty)
    if composerEmpty is not true or afterCount ≤ beforeCount or bodyCount is not 1 then return false
    considering case
        if bodyText is not expectedText then return false
    end considering
    if tableWidth ≤ 0 or bodyWidth ≤ 0 then return false
    set tableRight to tableX + tableWidth
    set bodyRight to bodyX + bodyWidth
    if bodyX < tableX or bodyRight > tableRight then return false
    -- This is a conservative local right-side receipt, not remote delivery.
    if bodyX + bodyWidth / 2 ≤ tableX + tableWidth * 0.55 then return false
    if bodyRight < tableX + tableWidth * 0.8 then return false
    return true
end observedOutgoingReceipt

on sendRowSignature(messageRow)
    set signatureParts to {}
    repeat with elementRef in (my allContents(messageRow))
        set rowElement to contents of elementRef
        set rowRole to my elementRole(rowElement)
        if rowRole is "AXTextArea" or rowRole is "AXStaticText" then
            set end of signatureParts to {rowRole, my elementValue(rowElement)}
        end if
    end repeat
    return signatureParts
end sendRowSignature

on sendTableSignatures(messageTable)
    tell application "System Events" to set messageRows to rows of messageTable
    set signatures to {}
    repeat with rowRef in messageRows
        set end of signatures to my sendRowSignature(contents of rowRef)
    end repeat
    return signatures
end sendTableSignatures

on observedSendPrefix(beforeSignatures, afterSignatures)
    set beforeCount to count beforeSignatures
    if beforeCount is 0 or (count afterSignatures) ≤ beforeCount then return false
    set anchorSignature to item beforeCount of beforeSignatures
    if (count anchorSignature) is 0 then return false
    set anchorMatches to 0
    considering case
        repeat with rowIndex from 1 to beforeCount
            if item rowIndex of beforeSignatures is not item rowIndex of afterSignatures then return false
        end repeat
        repeat with signatureRef in afterSignatures
            if contents of signatureRef is anchorSignature then set anchorMatches to anchorMatches + 1
        end repeat
    end considering
    return anchorMatches is 1
end observedSendPrefix

on hasNewOutgoingReceipt(messageTable, composerElement, beforeSignatures, expectedText)
    set beforeCount to count beforeSignatures
    set composerEmpty to (my elementValue(composerElement)) is ""
    if composerEmpty is not true then return false
    tell application "System Events"
        set messageRows to rows of messageTable
    end tell
    set afterCount to count messageRows
    if afterCount ≤ beforeCount then return false
    if not my observedSendPrefix(beforeSignatures, my sendTableSignatures(messageTable)) then return false
    set lastRow to item afterCount of messageRows
    set bodyElements to {}
    repeat with elementRef in (my allContents(lastRow))
        set bodyElement to contents of elementRef
        if (my elementRole(bodyElement)) is "AXTextArea" then set end of bodyElements to bodyElement
    end repeat
    if (count bodyElements) is not 1 then return false
    set bodyElement to item 1 of bodyElements
    set tableRect to my sendElementRect(messageTable)
    set bodyRect to my sendElementRect(bodyElement)
    return my observedOutgoingReceipt(beforeCount, afterCount, count bodyElements, my elementValue(bodyElement), expectedText, item 1 of tableRect, item 3 of tableRect, item 1 of bodyRect, item 3 of bodyRect, composerEmpty)
end hasNewOutgoingReceipt

on sendOriginalReply(expectedTitle)
    set my nativeWriteStarted to false
    considering case
        if expectedTitle is not "Ri@微信" then error "TITLE_MISMATCH" number -2702
    end considering
    set replyText to my decodedReplyText()
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
    set messageTable to my discoverSendTable(targetWindow)
    set beforeSignatures to my sendTableSignatures(messageTable)
    if (count beforeSignatures) is 0 then error "STRUCTURE_CHANGED" number -2704
    set anchorSignature to last item of beforeSignatures
    set anchorMatches to 0
    considering case
        repeat with signatureRef in beforeSignatures
            if contents of signatureRef is anchorSignature then set anchorMatches to anchorMatches + 1
        end repeat
    end considering
    if (count anchorSignature) is 0 or anchorMatches is not 1 then error "STRUCTURE_CHANGED" number -2704
    tell application "System Events"
        set frontmost of targetProcess to true
        set value of attribute "AXFocused" of composerElement to true
    end tell
    my requireSendFocus(targetProcess, composerElement)
    my validateReplyIdentity(targetProcess, targetWindow, windowCount, sidebarTable)
    if (my discoverComposer(targetWindow)) is not composerElement then error "COMPOSER_CHANGED" number -2714
    my validateComposer(composerElement, sidebarTable)
    set my nativeWriteStarted to true
    try
        tell application "System Events"
            set value of attribute "AXValue" of composerElement to replyText
        end tell
    on error
        error "DRAFT_WRITE_FAILED" number -2722
    end try
    considering case
        if (my elementValue(composerElement)) is not replyText then error "DRAFT_MISMATCH" number -2723
    end considering
    my requireSendFocus(targetProcess, composerElement)
    my validateReplyIdentity(targetProcess, targetWindow, windowCount, sidebarTable)
    if (my discoverComposer(targetWindow)) is not composerElement then error "COMPOSER_CHANGED" number -2714
    my requireSendFocus(targetProcess, composerElement)
    considering case
        if (my elementValue(composerElement)) is not replyText then error "DRAFT_MISMATCH" number -2723
    end considering
    tell application "System Events"
        key code 36
    end tell
    repeat 10 times
        try
            my validateReplyIdentity(targetProcess, targetWindow, windowCount, sidebarTable)
            if (my discoverComposer(targetWindow)) is not composerElement then error "COMPOSER_CHANGED" number -2714
            set currentTable to my discoverSendTable(targetWindow)
            if my hasNewOutgoingReceipt(currentTable, composerElement, beforeSignatures, replyText) then return "SENT"
        on error
            -- A receipt read may race layout updates. Only observe; never re-send.
        end try
        delay 0.1
    end repeat
    error "RECEIPT_UNCONFIRMED" number -2724
end sendOriginalReply

on safeSendCode(errorNumber)
    if errorNumber is -2701 or errorNumber is -1719 then return "NO_WINDOW"
    if errorNumber is -2702 then return "TITLE_MISMATCH"
    if errorNumber is -2703 then return "NOT_PERSONAL"
    if errorNumber is -2704 then return "STRUCTURE_CHANGED"
    if errorNumber is -2711 then return "COMPOSER_UNAVAILABLE"
    if errorNumber is -2712 then return "COMPOSER_AMBIGUOUS"
    if errorNumber is -2713 then return "COMPOSER_NOT_EMPTY"
    if errorNumber is -2714 then return "COMPOSER_CHANGED"
    if errorNumber is -2720 then return "EMPTY_CONTENT"
    if errorNumber is -2721 then return "FOCUS_FAILED"
    if errorNumber is -2722 then return "DRAFT_WRITE_FAILED"
    if errorNumber is -2723 then return "DRAFT_MISMATCH"
    if errorNumber is -2724 then return "RECEIPT_UNCONFIRMED"
    if errorNumber is -1743 or errorNumber is -25211 then return "PERMISSION_DENIED"
    return "SEND_FAILED"
end safeSendCode

on run argv
    if (count argv) is not 1 then return "ERROR" & tab & "TITLE_MISMATCH"
    try
        return my sendOriginalReply(item 1 of argv)
    on error errorText number errorNumber
        set safeCode to my safeSendCode(errorNumber)
        if my nativeWriteStarted then return "UNKNOWN" & tab & safeCode
        return "ERROR" & tab & safeCode
    end try
end run
