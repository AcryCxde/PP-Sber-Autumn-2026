-- One READ COMMITTED snapshot. OIDs are join keys only, never portable identities.
-- $1 is the installed query-bound profile JSON, not target-controlled SQL.
WITH
ns AS MATERIALIZED (
 SELECT oid, nspname FROM pg_namespace
 WHERE oid NOT IN ('pg_catalog'::regnamespace, 'information_schema'::regnamespace,
                   'pg_toast'::regnamespace)
 -- Match PG16 isTempNamespace/isAnyTempNamespace semantics, including stale
 -- namespaces. Do not exempt arbitrary pg_* names or nonpermanent objects:
 -- the closed audit below still refuses every active temporary object.
 AND oid<>pg_my_temp_schema() AND NOT pg_is_other_temp_schema(oid)
),
rels AS MATERIALIZED (
 SELECT c.*, n.nspname FROM pg_class c JOIN ns n ON n.oid=c.relnamespace
),
tabs AS MATERIALIZED (
 SELECT * FROM rels WHERE relkind='r' AND relpersistence='p'
),
cols AS MATERIALIZED (
 SELECT a.*, t.nspname, t.relname FROM pg_attribute a
 JOIN tabs t ON t.oid=a.attrelid WHERE a.attnum>0 AND NOT a.attisdropped
),
seqs AS MATERIALIZED (
 SELECT s.*, r.nspname, r.relname, r.oid AS relid,
        d.refobjid AS tabid, d.refobjsubid AS attnum, d.deptype,
        t.nspname AS tabns, t.relname AS tabname, a.attname, ad.oid AS defid,
        pg_get_expr(ad.adbin,ad.adrelid,false) AS defexpr
 FROM pg_sequence s JOIN rels r ON r.oid=s.seqrelid
 LEFT JOIN pg_depend d ON d.classid='pg_class'::regclass AND d.objid=r.oid
  AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i')
 LEFT JOIN tabs t ON t.oid=d.refobjid
 LEFT JOIN cols a ON a.attrelid=t.oid AND a.attnum=d.refobjsubid
 LEFT JOIN pg_attrdef ad ON ad.adrelid=t.oid AND ad.adnum=a.attnum
),
endpoints AS MATERIALIZED (
 SELECT 'pg_namespace'::regclass AS classid,oid AS objid,0 AS subid,
  'schemas'::text AS kind,jsonb_build_array(nspname) AS identity FROM ns
 UNION ALL SELECT 'pg_class'::regclass,oid,0,'tables',jsonb_build_array(nspname,relname) FROM tabs
 UNION ALL SELECT 'pg_class'::regclass,attrelid,attnum,'columns',jsonb_build_array(nspname,relname,attname) FROM cols
 UNION ALL SELECT 'pg_attrdef'::regclass,d.oid,0,'columns',jsonb_build_array(a.nspname,a.relname,a.attname)
  FROM pg_attrdef d JOIN cols a ON a.attrelid=d.adrelid AND a.attnum=d.adnum
 UNION ALL SELECT 'pg_class'::regclass,r.oid,0,'indexes',jsonb_build_array(r.nspname,r.relname)
  FROM rels r JOIN pg_index i ON i.indexrelid=r.oid JOIN tabs t ON t.oid=i.indrelid
 UNION ALL SELECT 'pg_class'::regclass,relid,0,'sequences',jsonb_build_array(nspname,relname) FROM seqs
 UNION ALL SELECT 'pg_constraint'::regclass,c.oid,0,'constraints',jsonb_build_array(t.nspname,t.relname,c.conname)
  FROM pg_constraint c JOIN tabs t ON t.oid=c.conrelid
),
derived AS MATERIALIZED (
 SELECT 'pg_type'::regclass AS classid,ty.oid AS objid FROM pg_type ty JOIN tabs t ON t.reltype=ty.oid
 UNION SELECT 'pg_type'::regclass,ty.typarray FROM pg_type ty JOIN tabs t ON t.reltype=ty.oid
 UNION SELECT 'pg_trigger'::regclass,tr.oid FROM pg_trigger tr JOIN pg_constraint c ON c.oid=tr.tgconstraint
  JOIN tabs t ON t.oid=c.conrelid WHERE tr.tgisinternal AND c.contype IN ('f','u','p') AND tr.tgenabled='O'
 UNION SELECT 'pg_class'::regclass,t.reltoastrelid FROM tabs t WHERE t.reltoastrelid<>0
 UNION SELECT 'pg_class'::regclass,i.indexrelid FROM pg_index i JOIN tabs t ON t.reltoastrelid=i.indrelid
 UNION SELECT 'pg_type'::regclass,c.reltype FROM pg_class c JOIN tabs t ON t.reltoastrelid=c.oid
),
-- Reviewed REL_16_15 src/pl/plpgsql/src/plpgsql--1.0.sql profile.
-- Membership is exact, not permission for arbitrary objects added to plpgsql.
plpgsql_functions AS MATERIALIZED (
 SELECT p.oid FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang
 JOIN (VALUES ('plpgsql_call_handler',''::oidvector,'language_handler'::regtype,false),
              ('plpgsql_inline_handler',('internal'::regtype::oid::text)::oidvector,'void'::regtype,true),
              ('plpgsql_validator',('oid'::regtype::oid::text)::oidvector,'void'::regtype,true))
  v(name,args,result,strict) ON p.proname=v.name AND p.proargtypes=v.args
 WHERE p.pronamespace='pg_catalog'::regnamespace AND l.lanname='c'
  AND p.prorettype=v.result AND p.proisstrict=v.strict AND p.prokind='f'
  AND NOT p.proretset AND NOT p.prosecdef AND NOT p.proleakproof
  AND p.provolatile='v' AND p.proparallel='u'
  AND p.prosrc=p.proname AND p.probin='$libdir/plpgsql'
  AND p.proconfig IS NULL AND p.prosqlbody IS NULL
),
plpgsql_members AS MATERIALIZED (
 SELECT 'pg_proc'::regclass AS classid,oid AS objid FROM plpgsql_functions
 UNION ALL SELECT 'pg_language'::regclass,l.oid FROM pg_language l
 WHERE l.lanname='plpgsql' AND l.lanispl AND l.lanpltrusted
  AND l.lanplcallfoid=(SELECT oid FROM plpgsql_functions WHERE oid='pg_catalog.plpgsql_call_handler()'::regprocedure)
  AND l.laninline=(SELECT oid FROM plpgsql_functions WHERE oid='pg_catalog.plpgsql_inline_handler(internal)'::regprocedure)
  AND l.lanvalidator=(SELECT oid FROM plpgsql_functions WHERE oid='pg_catalog.plpgsql_validator(oid)'::regprocedure)
),
plpgsql AS MATERIALIZED (
 SELECT 'pg_extension'::regclass AS classid,e.oid AS objid FROM pg_extension e
 WHERE e.extname='plpgsql' AND e.extversion='1.0' AND e.extnamespace='pg_catalog'::regnamespace
  AND NOT e.extrelocatable AND e.extconfig IS NULL AND e.extcondition IS NULL
 UNION ALL SELECT classid,objid FROM plpgsql_members
),
-- PG16.15 tablecmds.c/index.c: AFTER ROW FK checks/actions and deferred
-- unique rechecks. Compare semantic fields and multiplicity, never generated names.
expected_triggers AS MATERIALIZED (
 SELECT c.oid AS constraintid,v.relid,v.otherrel,c.conindid AS indexid,
  v.func,v.event,v.can_defer,v.initially_deferred
 FROM pg_constraint c JOIN tabs t ON t.oid=c.conrelid
 CROSS JOIN LATERAL (VALUES
  (c.conrelid,c.confrelid,'RI_FKey_check_ins',5,c.condeferrable,c.condeferred),
  (c.conrelid,c.confrelid,'RI_FKey_check_upd',17,c.condeferrable,c.condeferred),
  (c.confrelid,c.conrelid,'RI_FKey_' || CASE c.confdeltype WHEN 'a' THEN 'noaction' WHEN 'r' THEN 'restrict'
    WHEN 'c' THEN 'cascade' WHEN 'n' THEN 'setnull' WHEN 'd' THEN 'setdefault' END || '_del',9,
    c.condeferrable AND c.confdeltype='a',c.condeferred AND c.confdeltype='a'),
  (c.confrelid,c.conrelid,'RI_FKey_' || CASE c.confupdtype WHEN 'a' THEN 'noaction' WHEN 'r' THEN 'restrict'
    WHEN 'c' THEN 'cascade' WHEN 'n' THEN 'setnull' WHEN 'd' THEN 'setdefault' END || '_upd',17,
    c.condeferrable AND c.confupdtype='a',c.condeferred AND c.confupdtype='a')
 ) v(relid,otherrel,func,event,can_defer,initially_deferred) WHERE c.contype='f'
 UNION ALL
 SELECT c.oid,c.conrelid,0,c.conindid,'unique_key_recheck',21,c.condeferrable,c.condeferred
 FROM pg_constraint c JOIN tabs t ON t.oid=c.conrelid WHERE c.contype IN ('p','u') AND c.condeferrable
),
verified_triggers AS MATERIALIZED (
 SELECT tr.oid,e.constraintid,e.relid,e.func,e.event FROM pg_trigger tr
 JOIN pg_proc p ON p.oid=tr.tgfoid AND p.pronamespace='pg_catalog'::regnamespace
  AND p.proargtypes=''::oidvector AND p.prorettype='trigger'::regtype AND p.oid<16384
 JOIN expected_triggers e ON (tr.tgconstraint,tr.tgrelid,tr.tgconstrrelid,tr.tgconstrindid,
  p.proname,tr.tgtype,tr.tgdeferrable,tr.tginitdeferred)=
  (e.constraintid,e.relid,e.otherrel,e.indexid,e.func,e.event,e.can_defer,e.initially_deferred)
 WHERE tr.tgisinternal AND tr.tgenabled='O' AND tr.tgqual IS NULL
  AND tr.tgnargs=0 AND tr.tgargs=''::bytea AND tr.tgattr=''::int2vector
  AND tr.tgparentid=0 AND tr.tgoldtable IS NULL AND tr.tgnewtable IS NULL
),
allowed AS MATERIALIZED (
 SELECT classid,objid FROM endpoints UNION SELECT classid,objid FROM derived UNION SELECT classid,objid FROM plpgsql
),
edges AS (
 SELECT DISTINCT s.kind AS source,s.identity AS source_id,t.kind AS target,t.identity AS target_id,d.deptype::text AS dependency
 FROM pg_depend d JOIN endpoints s ON (s.classid,s.objid,s.subid)=(d.classid,d.objid,d.objsubid)
 JOIN endpoints t ON (t.classid,t.objid,t.subid)=(d.refclassid,d.refobjid,d.refobjsubid)
),
records AS (
 SELECT 'dependency_edges' AS section,jsonb_build_object('identity',jsonb_build_array(source,source_id,target,target_id),
        'dependency',dependency) AS value,source_id::text AS sort1,target_id::text AS sort2,0 AS pos FROM edges
 UNION ALL
 SELECT 'schemas' AS section, jsonb_build_object('identity',jsonb_build_array(nspname)) AS value,
        nspname::text AS sort1, ''::text AS sort2, 0 AS pos FROM ns
 UNION ALL
 SELECT 'tables', jsonb_build_object('identity',jsonb_build_array(t.nspname,t.relname),
        'access_method',am.amname), t.nspname,t.relname,0
 FROM tabs t JOIN pg_am am ON am.oid=t.relam
 UNION ALL
 SELECT 'columns',jsonb_build_object('identity',jsonb_build_array(a.nspname,a.relname,a.attname),
        'position',a.attnum,'type',jsonb_build_array(tn.nspname,ty.typname),
        'typmod',a.atttypmod,'formatted_type',format_type(a.atttypid,a.atttypmod),
        'collation',CASE WHEN co.oid IS NOT NULL THEN jsonb_build_array(cn.nspname,co.collname) END,
        'not_null',a.attnotnull,'default',pg_get_expr(ad.adbin,ad.adrelid,false)),
        a.nspname,a.relname,a.attnum
 FROM cols a JOIN pg_type ty ON ty.oid=a.atttypid JOIN pg_namespace tn ON tn.oid=ty.typnamespace
 LEFT JOIN pg_collation co ON co.oid=a.attcollation
 LEFT JOIN pg_namespace cn ON cn.oid=co.collnamespace
 LEFT JOIN pg_attrdef ad ON ad.adrelid=a.attrelid AND ad.adnum=a.attnum
 UNION ALL
 SELECT 'constraints',jsonb_build_object('identity',jsonb_build_array(t.nspname,t.relname,c.conname),
        'kind',c.contype,'columns',COALESCE((SELECT jsonb_agg(jsonb_build_array(t.nspname,t.relname,a.attname) ORDER BY k.ord)
          FROM unnest(c.conkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.num),'[]'::jsonb),
        'referenced_table',CASE WHEN rt.oid IS NOT NULL THEN jsonb_build_array(rn.nspname,rt.relname) END,
        'referenced_columns',COALESCE((SELECT jsonb_agg(jsonb_build_array(rn.nspname,rt.relname,a.attname) ORDER BY k.ord)
          FROM unnest(c.confkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.num),'[]'::jsonb),
        'match',CASE WHEN c.contype='f' THEN c.confmatchtype::text END,
        'on_update',CASE WHEN c.contype='f' THEN c.confupdtype::text END,
        'on_delete',CASE WHEN c.contype='f' THEN c.confdeltype::text END,
        'deferrable',c.condeferrable,'deferred',c.condeferred,'validated',c.convalidated,
        'no_inherit',c.connoinherit,'definition',pg_get_constraintdef(c.oid,false)),t.nspname,t.relname,0
 FROM pg_constraint c JOIN tabs t ON t.oid=c.conrelid
 LEFT JOIN pg_class rt ON rt.oid=c.confrelid LEFT JOIN pg_namespace rn ON rn.oid=rt.relnamespace
 UNION ALL
 SELECT 'indexes',jsonb_build_object('identity',jsonb_build_array(r.nspname,r.relname),
        'table',jsonb_build_array(t.nspname,t.relname),'method',am.amname,
        'unique',i.indisunique,'nulls_not_distinct',i.indnullsnotdistinct,
        'keys',(SELECT jsonb_agg(jsonb_build_object(
          'definition',pg_get_indexdef(i.indexrelid,k+1,false),
          'column',CASE WHEN a.attname IS NOT NULL THEN jsonb_build_array(t.nspname,t.relname,a.attname) END,
          'collation',CASE WHEN co.oid IS NOT NULL THEN jsonb_build_array(cn.nspname,co.collname) END,
          'opclass',jsonb_build_array(onsp.nspname,op.opcname),
          'descending',(i.indoption[k] & 1)=1,'nulls_first',(i.indoption[k] & 2)=2) ORDER BY k)
          FROM generate_series(0,i.indnkeyatts-1) k
          LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=i.indkey[k]
          JOIN pg_opclass op ON op.oid=i.indclass[k] JOIN pg_namespace onsp ON onsp.oid=op.opcnamespace
          LEFT JOIN pg_collation co ON co.oid=i.indcollation[k]
          LEFT JOIN pg_namespace cn ON cn.oid=co.collnamespace),
        'include',COALESCE((SELECT jsonb_agg(jsonb_build_array(t.nspname,t.relname,a.attname) ORDER BY k)
          FROM generate_series(i.indnkeyatts,i.indnatts-1) k
          JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=i.indkey[k]),'[]'::jsonb),
        'expressions',pg_get_expr(i.indexprs,i.indrelid,false),
        'predicate',pg_get_expr(i.indpred,i.indrelid,false),
        'options',COALESCE((SELECT jsonb_object_agg(split_part(opt,'=',1),
          CASE WHEN split_part(opt,'=',1)='fillfactor' THEN to_jsonb(split_part(opt,'=',2)::integer)
               WHEN split_part(opt,'=',1)='deduplicate_items' THEN to_jsonb(split_part(opt,'=',2)::boolean)
               ELSE to_jsonb(split_part(opt,'=',2)) END) FROM unnest(r.reloptions) opt),'{}'::jsonb),
        'valid',i.indisvalid,'ready',i.indisready,'live',i.indislive),r.nspname,r.relname,0
 FROM pg_index i JOIN tabs t ON t.oid=i.indrelid JOIN rels r ON r.oid=i.indexrelid
 JOIN pg_am am ON am.oid=r.relam
 UNION ALL
 SELECT 'sequences',jsonb_build_object('identity',jsonb_build_array(s.nspname,s.relname),
        'type',jsonb_build_array(tn.nspname,ty.typname),'start',s.seqstart,'increment',s.seqincrement,
        'min',s.seqmin,'max',s.seqmax,'cache',s.seqcache,'cycle',s.seqcycle,
        'owned_by',jsonb_build_array(s.tabns,s.tabname,s.attname),
        'ownership_dependency',s.deptype,'default',s.defexpr),s.nspname,s.relname,0
 FROM seqs s JOIN pg_type ty ON ty.oid=s.seqtypid JOIN pg_namespace tn ON tn.oid=ty.typnamespace
),
inventory AS (
 SELECT jsonb_object_agg(section,items) AS data FROM (
 SELECT section,COALESCE(jsonb_agg(value ORDER BY sort1,sort2,pos) FILTER (WHERE value IS NOT NULL),'[]'::jsonb) AS items
 FROM unnest(ARRAY['schemas','tables','columns','constraints','indexes','sequences','dependency_edges']) section
 LEFT JOIN records USING(section) GROUP BY section) a
)
,
-- Closed PG16 class audit. Built-in objects use initdb's reserved OID range,
-- not a pg_* name exemption. Derived objects have explicit parent checks.
audit AS (
 SELECT 'pg_namespace' AS class,0::bigint AS n
 UNION ALL SELECT 'pg_class',count(*) FROM pg_class r WHERE r.oid>=16384 AND (
  NOT EXISTS(SELECT FROM allowed a WHERE a.classid='pg_class'::regclass AND a.objid=r.oid)
  OR r.relpersistence<>'p'
  OR (r.relkind='r' AND (r.relrowsecurity OR r.relforcerowsecurity OR r.reloptions IS NOT NULL)))
 UNION ALL SELECT 'pg_attribute',count(*) FROM cols a JOIN pg_type ty ON ty.oid=a.atttypid
  WHERE a.attidentity<>'' OR a.attgenerated<>''
   -- Derived row/array metadata is allowed to exist, never to widen column types.
   OR NOT (ty.oid<16384 AND ty.typnamespace='pg_catalog'::regnamespace AND ty.typtype='b')
 UNION ALL SELECT 'pg_attrdef',count(*) FROM pg_attrdef d WHERE d.oid>=16384 AND NOT EXISTS(
  SELECT FROM cols a WHERE a.attrelid=d.adrelid AND a.attnum=d.adnum)
 UNION ALL SELECT 'pg_type',count(*) FROM pg_type ty WHERE ty.oid>=16384 AND NOT (
  (ty.typtype='c' AND EXISTS(SELECT FROM tabs t WHERE t.reltype=ty.oid)) OR
  (ty.typelem<>0 AND EXISTS(SELECT FROM tabs t JOIN pg_type base ON base.oid=t.reltype WHERE base.typarray=ty.oid)))
 UNION ALL SELECT 'pg_constraint',count(*) FROM pg_constraint c WHERE c.oid>=16384
  AND NOT (c.contype IN ('p','u','f','c') AND EXISTS(SELECT FROM tabs t WHERE t.oid=c.conrelid))
 UNION ALL SELECT 'pg_index',count(*) FROM pg_index i JOIN rels r ON r.oid=i.indexrelid
  WHERE NOT i.indisvalid OR NOT i.indisready OR NOT i.indislive
 UNION ALL SELECT 'pg_am',count(*) FROM pg_am WHERE oid>=16384
 UNION ALL SELECT 'pg_opclass',count(*) FROM pg_opclass WHERE oid>=16384
 UNION ALL SELECT 'pg_opfamily',count(*) FROM pg_opfamily WHERE oid>=16384
 UNION ALL SELECT 'pg_collation',count(*) FROM pg_collation WHERE oid>=16384
 UNION ALL SELECT 'pg_inherits',count(*) FROM pg_inherits
 UNION ALL SELECT 'pg_partitioned_table',count(*) FROM pg_partitioned_table
 UNION ALL SELECT 'pg_sequence',count(*) FROM seqs s WHERE
  s.deptype IS DISTINCT FROM 'a' OR s.attname IS NULL OR s.defexpr IS DISTINCT FROM
   ('nextval(' || quote_literal(s.relid::regclass::text) || '::regclass)')
  OR NOT EXISTS(SELECT FROM pg_depend d WHERE d.classid='pg_attrdef'::regclass
   AND d.objid=s.defid AND d.refclassid='pg_class'::regclass AND d.refobjid=s.relid AND d.deptype='n')
 UNION ALL SELECT 'pg_depend',count(*) FROM pg_depend d WHERE
  (EXISTS(SELECT FROM allowed a WHERE (a.classid,a.objid)=(d.classid,d.objid)) OR
   EXISTS(SELECT FROM allowed a WHERE (a.classid,a.objid)=(d.refclassid,d.refobjid))) AND (
   d.deptype NOT IN ('a','n','i','e') OR
   (d.objid>=16384 AND NOT EXISTS(SELECT FROM allowed a WHERE (a.classid,a.objid)=(d.classid,d.objid))) OR
   (d.refobjid>=16384 AND NOT EXISTS(SELECT FROM allowed a WHERE (a.classid,a.objid)=(d.refclassid,d.refobjid))))
 UNION ALL SELECT 'pg_extension',
  (SELECT count(*) FROM pg_extension e WHERE NOT EXISTS(
    SELECT FROM plpgsql p WHERE p.classid='pg_extension'::regclass AND p.objid=e.oid))
  + CASE WHEN (SELECT count(*) FROM plpgsql WHERE classid='pg_extension'::regclass)=1
    AND (SELECT count(*) FROM plpgsql_members)=4 THEN 0 ELSE 1 END
  + (SELECT count(*) FROM pg_depend d JOIN pg_extension e ON e.oid=d.refobjid
     WHERE d.refclassid='pg_extension'::regclass AND e.extname='plpgsql' AND d.deptype='e'
      AND NOT EXISTS(SELECT FROM plpgsql_members m
       WHERE (m.classid,m.objid,0)=(d.classid,d.objid,d.objsubid)))
  + (SELECT count(*) FROM plpgsql_members m WHERE NOT EXISTS(
     SELECT FROM pg_depend d JOIN pg_extension e ON e.oid=d.refobjid
     WHERE d.refclassid='pg_extension'::regclass AND e.extname='plpgsql' AND d.deptype='e'
      AND (d.classid,d.objid,d.objsubid)=(m.classid,m.objid,0)))
 UNION ALL SELECT 'pg_proc',count(*) FROM pg_proc p WHERE p.oid>=16384 AND NOT EXISTS(
  SELECT FROM pg_depend d JOIN pg_extension e ON e.oid=d.refobjid
  WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.refclassid='pg_extension'::regclass
   AND d.deptype='e' AND e.extname='plpgsql' AND p.pronamespace='pg_catalog'::regnamespace
   AND p.proname IN ('plpgsql_call_handler','plpgsql_inline_handler','plpgsql_validator'))
 UNION ALL SELECT 'pg_trigger',
  (SELECT count(*) FROM pg_trigger tr WHERE NOT EXISTS(SELECT FROM verified_triggers v WHERE v.oid=tr.oid))
  + (SELECT count(*) FROM expected_triggers e WHERE
    (SELECT count(*) FROM verified_triggers v WHERE
     (v.constraintid,v.relid,v.func,v.event)=(e.constraintid,e.relid,e.func,e.event))<>1)
 UNION ALL SELECT 'pg_rewrite',count(*) FROM pg_rewrite WHERE oid>=16384
 UNION ALL SELECT 'pg_policy',count(*) FROM pg_policy
 UNION ALL SELECT 'pg_publication',count(*) FROM pg_publication
 UNION ALL SELECT 'pg_publication_rel',count(*) FROM pg_publication_rel
 UNION ALL SELECT 'pg_publication_namespace',count(*) FROM pg_publication_namespace
 UNION ALL SELECT 'pg_subscription',count(subdbid) FROM pg_subscription
 UNION ALL SELECT 'pg_subscription_rel',count(*) FROM pg_subscription_rel
 UNION ALL SELECT 'pg_event_trigger',count(*) FROM pg_event_trigger
 UNION ALL SELECT 'pg_foreign_table',count(*) FROM pg_foreign_table
 UNION ALL SELECT 'pg_foreign_server',count(*) FROM pg_foreign_server
 UNION ALL SELECT 'pg_foreign_data_wrapper',count(*) FROM pg_foreign_data_wrapper
 UNION ALL SELECT 'pg_user_mapping',count(umid) FROM pg_user_mappings
 UNION ALL SELECT 'pg_conversion',count(*) FROM pg_conversion WHERE oid>=16384
 UNION ALL SELECT 'pg_operator',count(*) FROM pg_operator WHERE oid>=16384
 UNION ALL SELECT 'pg_cast',count(*) FROM pg_cast WHERE oid>=16384
 UNION ALL SELECT 'pg_transform',count(*) FROM pg_transform
 UNION ALL SELECT 'pg_language',count(*) FROM pg_language WHERE lanname NOT IN ('internal','c','sql','plpgsql')
 UNION ALL SELECT 'pg_ts_config',count(*) FROM pg_ts_config WHERE oid>=16384
 UNION ALL SELECT 'pg_ts_config_map',count(*) FROM pg_ts_config_map WHERE mapcfg>=16384
 UNION ALL SELECT 'pg_ts_dict',count(*) FROM pg_ts_dict WHERE oid>=16384
 UNION ALL SELECT 'pg_ts_parser',count(*) FROM pg_ts_parser WHERE oid>=16384
 UNION ALL SELECT 'pg_ts_template',count(*) FROM pg_ts_template WHERE oid>=16384
 UNION ALL SELECT 'pg_statistic_ext',count(*) FROM pg_statistic_ext
 UNION ALL SELECT 'pg_default_acl',count(*) FROM pg_default_acl
 UNION ALL SELECT 'pg_init_privs',count(*) FROM pg_init_privs WHERE objoid>=16384 AND NOT (
  classoid='pg_proc'::regclass AND EXISTS(SELECT FROM pg_proc p WHERE p.oid=objoid
   AND p.proname IN ('plpgsql_call_handler','plpgsql_inline_handler','plpgsql_validator')
   AND p.pronamespace='pg_catalog'::regnamespace))
 UNION ALL SELECT 'pg_shdepend',count(*) FROM pg_shdepend WHERE deptype NOT IN ('o','a')
 UNION ALL SELECT 'pg_database',count(*) FROM pg_database WHERE datname=current_database() AND (
  pg_get_userbyid(datdba)=current_user OR has_database_privilege(current_user,oid,'CREATE')
  OR has_database_privilege(current_user,oid,'TEMP'))
 UNION ALL SELECT 'pg_authid',count(*) FROM pg_roles WHERE rolname=current_user AND (
  rolsuper OR rolinherit OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls)
 UNION ALL SELECT 'pg_auth_members',count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname=current_user
),
snapshot AS (
 SELECT jsonb_build_object('profile',$1::jsonb,
  'facts',jsonb_build_object('database',current_database(),'current_user',current_user,
   'session_user',session_user,'server_version_num',current_setting('server_version_num')::integer,
   'server_address',host(inet_server_addr()),'server_port',inet_server_port()),
  -- pg_catalog.default is a symbolic identity, not its database-local semantics.
  -- Keep locale/provider/version in the portable schema, never endpoint facts.
  'database_collation',(SELECT jsonb_build_object(
   'encoding',pg_encoding_to_char(encoding),'provider',datlocprovider::text,
   'collate',datcollate,'ctype',datctype,'icu_locale',daticulocale,
   'icu_rules',daticurules,'version',datcollversion,
   'actual_version',pg_database_collation_actual_version(oid))
   FROM pg_database WHERE datname=current_database()),
  'inventory',data,'unsupported',(SELECT jsonb_object_agg(class,n) FROM audit),
  'counts',(SELECT jsonb_object_agg(key,jsonb_array_length(value)) FROM jsonb_each(data)))::text AS payload
 FROM inventory
)
SELECT CASE WHEN octet_length(payload)>4194304 THEN '{"overflow":true}' ELSE payload END FROM snapshot;
