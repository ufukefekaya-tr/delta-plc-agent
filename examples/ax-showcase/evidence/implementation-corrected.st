(* Reviewed bounded internal counter; no physical I/O. *)
Permit := Enable AND NOT Inhibit;
IF Permit AND (Counter < Limit) THEN
    Counter := Counter + 1;
END_IF;