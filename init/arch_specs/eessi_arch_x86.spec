# x86_64 CPU architecture specifications
# Software path in EESSI 	| Vendor ID 	| List of defining CPU features
# 2024-01-23: comment out haswell as it is not supported by NESSI and causes CI
# to fail
# "x86_64/intel/haswell"		"GenuineIntel"	"avx2 fma"		# Intel Haswell
"x86_64/intel/broadwell"	"GenuineIntel"	"avx2 fma rdseed adx"	# Intel Broadwell
"x86_64/intel/skylake_avx512"	"GenuineIntel"	"avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl"	# Intel Skylake, Cascade Lake
"x86_64/amd/zen2"		"AuthenticAMD"	"avx2 fma"		# AMD Rome
# "x86_64/amd/zen3"		"AuthenticAMD"	"avx2 fma vaes"		# AMD Milan, Milan-X
