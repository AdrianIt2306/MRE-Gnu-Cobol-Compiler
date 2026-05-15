       identification division.
       program-id. saldos.

       data division.
       working-storage section.

           exec sql include sqlca end-exec.

           EXEC SQL BEGIN DECLARE SECTION END-EXEC.
       01 hv-customers      PIC 9(9)   VALUE 0.
       01 cl_id       PIC 9(10)  VALUE 0.
       01 cl_doc_type    PIC X(10)  VALUE SPACES.
       01 cl_doc_num     PIC X(20)  VALUE SPACES.
       01 cl_full_name   PIC X(100) VALUE SPACES.
       01 cl_segment     PIC X(50)  VALUE SPACES.
       01 cl_created_at  PIC X(26)  VALUE SPACES.
           EXEC SQL END DECLARE SECTION END-EXEC.

       procedure division.
       inicio.
           EXEC SQL
               SELECT COUNT(*)
                 INTO :hv-customers
                 FROM customers
           END-EXEC

           if sqlcode not = 0
               display "Error SELECT SQLCODE=" sqlcode
               display "SQLSTATE=" sqlstate
           else
               display "CUSTOMERS totals: " hv-customers
           end-if


      *--- Mostrar todos los registros de customers ---*

           EXEC SQL
               DECLARE c1 CURSOR FOR
                   SELECT cl_id, cl_doc_type, cl_doc_num,cl_full_name,
                   cl_segment,cl_created_at
                   FROM customers
           END-EXEC

           EXEC SQL OPEN c1 END-EXEC

           if sqlcode not = 0
               display "OPEN CURSOR SQLCODE=" sqlcode
               display "OPEN CURSOR SQLSTATE=" sqlstate
               stop run
           end-if

           EXEC SQL
               FETCH c1 INTO
                   :cl_id,
                   :cl_doc_type,
                   :cl_doc_num,
                   :cl_full_name,
                   :cl_segment,
                   :cl_created_at
           END-EXEC
           
           perform until sqlcode not = 0
               if sqlcode = 0
                   display cl_id ' | '
                           cl_doc_type ' | '
                           cl_doc_num ' | '
                           cl_full_name(1:20) ' | '
                           cl_segment ' | '
                           cl_created_at
                   EXEC SQL
                       FETCH c1 INTO
                           :cl_id,
                           :cl_doc_type,
                           :cl_doc_num,
                           :cl_full_name,
                           :cl_segment,
                           :cl_created_at
                   END-EXEC
               end-if
           end-perform

           EXEC SQL CLOSE c1 END-EXEC

           stop run.

       end program saldos.