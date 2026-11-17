% The pinned PeTTa supports git-import!/2..4, but no revision argument.
% Add the five-argument form for an already provisioned official Omega.
% Never clone, fetch, build or execute a model-selected command here.
:- use_module(library(process)).
:- use_module(library(readutil)).
:- use_module(library(http/json)).
:- use_module(library(crypto)).

'git-import!'(URL, Build, BaseDir, Revision, true) :-
    atom_string(URLAtom, URL),
    URLAtom == 'https://github.com/singnet/Omega.git',
    memberchk(Build, ['', ""]),
    directory_file_path(BaseDir, 'Omega', LocalDir),
    ( exists_directory(LocalDir) -> true
    ; throw(error(existence_error(pinned_omega_checkout, LocalDir), _)) ),
    directory_file_path(LocalDir, '.git', Metadata),
    directory_file_path(LocalDir, 'persistent_atp_runtime.json', Manifest),
    ( exists_file(Manifest) -> persistent_atp_verify_manifest(LocalDir, Manifest, Revision)
    ; true ),
    ( (exists_directory(Metadata); exists_file(Metadata)) ->
        persistent_atp_verify_git(LocalDir, Revision)
    ; exists_file(Manifest) -> true
    ; throw(error(existence_error(pinned_omega_metadata, Metadata), _)) ),
    ( library_path(LocalDir) -> true ; asserta(library_path(LocalDir)) ).

persistent_atp_verify_git(LocalDir, Revision) :-
    process_create(path(git), ['-C', LocalDir, 'rev-parse', 'HEAD'],
        [stdout(pipe(Out)), stderr(null), process(PID)]),
    read_string(Out, _, Text),
    close(Out),
    process_wait(PID, Status),
    normalize_space(atom(Actual), Text),
    ( Status == exit(0), Actual == Revision -> true
    ; throw(error(permission_error(load, pinned_omega_revision, Actual), _)) ).

persistent_atp_verify_manifest(LocalDir, Manifest, Revision) :-
    setup_call_cleanup(open(Manifest, read, Stream), json_read_dict(Stream, Data), close(Stream)),
    atom_string(Recorded, Data.revision),
    ( Recorded == Revision -> true
    ; throw(error(permission_error(load, pinned_omega_revision, Recorded), _)) ),
    Files = Data.files,
    ( get_dict('src/loop.metta', Files, _), get_dict('src/plugin.py', Files, _),
      get_dict('run.metta', Files, _), get_dict('persistent_atp_pinned_import.pl', Files, _) -> true
    ; throw(error(permission_error(load, pinned_omega_content, Manifest), _)) ),
    dict_pairs(Files, _, Pairs),
    forall(member(Name-Expected, Pairs),
        ( ( \+ is_absolute_file_name(Name), \+ sub_atom(Name, _, _, _, '..'),
            directory_file_path(LocalDir, Name, Path), exists_file(Path),
            crypto_file_hash(Path, Actual, [algorithm(sha256), encoding(octet)]), atom_string(Actual, Expected) ) -> true
        ; throw(error(permission_error(load, pinned_omega_content, Name), _)) )).
