% The pinned PeTTa supports git-import!/2..4, but no revision argument.
% Add the five-argument form for an already provisioned official Omega.
% Never clone, fetch, build or execute a model-selected command here.
:- use_module(library(process)).
:- use_module(library(readutil)).

'git-import!'(URL, Build, BaseDir, Revision, true) :-
    atom_string(URLAtom, URL),
    URLAtom == 'https://github.com/singnet/Omega.git',
    memberchk(Build, ['', ""]),
    directory_file_path(BaseDir, 'Omega', LocalDir),
    ( exists_directory(LocalDir) -> true
    ; throw(error(existence_error(pinned_omega_checkout, LocalDir), _)) ),
    directory_file_path(LocalDir, '.git', Metadata),
    ( (exists_directory(Metadata); exists_file(Metadata)) -> true
    ; throw(error(existence_error(pinned_omega_metadata, Metadata), _)) ),
    process_create(path(git), ['-C', LocalDir, 'rev-parse', 'HEAD'],
        [stdout(pipe(Out)), stderr(null), process(PID)]),
    read_string(Out, _, Text),
    close(Out),
    process_wait(PID, Status),
    normalize_space(atom(Actual), Text),
    ( Status == exit(0), Actual == Revision -> true
    ; throw(error(permission_error(load, pinned_omega_revision, Actual), _)) ),
    asserta(library_path(LocalDir)).
