# Tool ownership

Lantai Runtime owns orchestration and delivery. OfficeCLI writes only through its transaction boundary, python-docx handles explicit structured operations, and GenOffice hosts user editing. Every write starts from the current file and records hash and rollback evidence.
