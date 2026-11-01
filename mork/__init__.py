"""MORK integration for persistent-atp.

    backend     The live space: a ctypes bridge to libmork_ffi.so, and the
                `GraphView` the commit gate validates against and writes to.
    projector   A batch export: an event journal read as JSON, written out as
                a `.metta` file. Nothing reads it back.

Both build the same atoms, and `projector.core` owns the id helpers they share.
"""
