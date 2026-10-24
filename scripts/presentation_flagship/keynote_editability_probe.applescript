on run argv
	if (count of argv) is not 6 then error "usage: keynote_editability_probe.applescript INPUT OUTPUT LABEL TABLE_SLIDE CHART_SLIDE EXPECTED_SLIDES"

	set inputPath to item 1 of argv
	set outputPath to item 2 of argv
	set deckLabel to item 3 of argv
	set tableSlideNumber to (item 4 of argv) as integer
	set chartSlideNumber to (item 5 of argv) as integer
	set expectedSlides to (item 6 of argv) as integer
	set markerText to " [EDITABILITY_PROBE_" & deckLabel & "]"
	set tableMarker to "EDITED_" & deckLabel
	set textCarrier to ""
	set originalChartWidth to 0
	set expectedChartWidth to 0
	set originalTableValue to ""

	tell application "Keynote"
		activate
		set sourceDocument to open (POSIX file inputPath)
	end tell
	delay 3

	tell application "Keynote"
		tell sourceDocument
			if (count of slides) is not expectedSlides then error "unexpected slide count before edit"

			tell slide 1
				if (count of text items) > 0 then
					set textCarrier to "text-item"
					set originalText to (object text of text item 1) as text
					set object text of text item 1 to originalText & markerText
				else if (count of shapes) > 0 then
					set textCarrier to "shape"
					set originalText to (object text of shape 1) as text
					set object text of shape 1 to originalText & markerText
				else
					error "no editable text carrier on slide 1"
				end if
			end tell

			if tableSlideNumber > 0 then
				tell slide tableSlideNumber
					if (count of tables) is 0 then error "expected editable table is missing"
					tell table 1
						if (count of cells) < 2 then error "expected table cells are missing"
						set originalTableValue to (formatted value of cell 2) as text
						set value of cell 2 to tableMarker
					end tell
				end tell
			end if

			if chartSlideNumber > 0 then
				tell slide chartSlideNumber
					if (count of charts) is 0 then error "expected editable chart is missing"
					set originalChartWidth to width of chart 1
					set expectedChartWidth to originalChartWidth + 4
					set width of chart 1 to expectedChartWidth
				end tell
			end if
		end tell

		export sourceDocument to (POSIX file outputPath) as Microsoft PowerPoint
		close sourceDocument saving no
	end tell
	delay 3

	tell application "Keynote"
		set roundTripDocument to open (POSIX file outputPath)
	end tell
	delay 3

	set markerPersisted to false
	set tablePersisted to false
	set chartPersisted to false
	set roundTripChartWidth to 0

	tell application "Keynote"
		tell roundTripDocument
			if (count of slides) is not expectedSlides then error "unexpected slide count after round trip"

			tell slide 1
				repeat with textObject in text items
					if ((object text of textObject) as text) contains markerText then set markerPersisted to true
				end repeat
				repeat with shapeObject in shapes
					if ((object text of shapeObject) as text) contains markerText then set markerPersisted to true
				end repeat
			end tell

			if tableSlideNumber > 0 then
				tell slide tableSlideNumber
					if (count of tables) > 0 then
						repeat with tableCell in cells of table 1
							if ((formatted value of tableCell) as text) is tableMarker then set tablePersisted to true
						end repeat
					end if
				end tell
			else
				set tablePersisted to true
			end if

			if chartSlideNumber > 0 then
				tell slide chartSlideNumber
					if (count of charts) > 0 then
						set roundTripChartWidth to width of chart 1
						if roundTripChartWidth is expectedChartWidth then set chartPersisted to true
					end if
				end tell
			else
				set chartPersisted to true
			end if
		end tell
		close roundTripDocument saving no
	end tell

	if markerPersisted is false then error "text edit did not survive Keynote PPTX round trip"
	if tablePersisted is false then error "table edit did not survive Keynote PPTX round trip"
	if chartPersisted is false then error "chart edit did not survive Keynote PPTX round trip"

	return "{\"deck\":\"" & deckLabel & "\",\"slides\":" & expectedSlides & ",\"text_carrier\":\"" & textCarrier & "\",\"text_edit_persisted\":true,\"table_slide\":" & tableSlideNumber & ",\"table_edit_persisted\":" & tablePersisted & ",\"table_original_value\":\"" & originalTableValue & "\",\"chart_slide\":" & chartSlideNumber & ",\"chart_original_width\":" & originalChartWidth & ",\"chart_expected_width\":" & expectedChartWidth & ",\"chart_round_trip_width\":" & roundTripChartWidth & ",\"chart_edit_persisted\":" & chartPersisted & "}"
end run
