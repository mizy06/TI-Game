	.file	"waveform_analyzer.c"
	.text
	.p2align 4
	.type	make_template, @function
make_template:
.LFB25:
	.cfi_startproc
	pushq	%r15
	.cfi_def_cfa_offset 16
	.cfi_offset 15, -16
	movl	%ecx, %eax
	movl	%edx, %r11d
	movl	$128, %r10d
	pushq	%r14
	.cfi_def_cfa_offset 24
	.cfi_offset 14, -24
	shrl	%eax
	movl	%ecx, %r14d
	leaq	g_template(%rip), %rcx
	pushq	%r13
	.cfi_def_cfa_offset 32
	.cfi_offset 13, -32
	movl	%esi, %r13d
	leaq	256(%rcx), %r9
	pushq	%r12
	.cfi_def_cfa_offset 40
	.cfi_offset 12, -40
	movl	%edx, %r12d
	subl	%eax, %r13d
	pushq	%rbp
	.cfi_def_cfa_offset 48
	.cfi_offset 6, -48
	shrl	%r12d
	leaq	g_sine_template_q10(%rip), %rbp
	pushq	%rbx
	.cfi_def_cfa_offset 56
	.cfi_offset 3, -56
	movl	%esi, %ebx
	movl	%eax, -4(%rsp)
	jmp	.L18
	.p2align 4,,10
	.p2align 3
.L37:
	testl	%edi, %edi
	je	.L34
	leaq	1(%rdx,%rdx), %rsi
	andl	$511, %esi
	imulq	$500000, %rsi, %rsi
	shrq	$7, %rsi
	cmpq	$500000, %rsi
	leal	-1000000(%rsi), %eax
	movl	%esi, %edx
	cmovbe	%esi, %eax
	subl	%ebx, %edx
	cmpl	$500000, %edx
	jle	.L9
	subl	$1000000, %edx
.L10:
	testl	%r11d, %r11d
	je	.L11
	movl	%eax, %r15d
	negl	%r15d
	cmovs	%eax, %r15d
	cmpl	%r15d, %r12d
	jnb	.L35
	testl	%r14d, %r14d
	je	.L15
	movl	%edx, %eax
	negl	%eax
	cmovs	%edx, %eax
	cmpl	%eax, -4(%rsp)
	jnb	.L14
.L15:
	cmpl	%esi, %r12d
	jnb	.L20
.L13:
	cmpl	%r13d, %esi
	sbbl	%eax, %eax
	andw	$2048, %ax
	subw	$1024, %ax
.L7:
	movw	%ax, (%rcx)
	addq	$2, %rcx
	addl	$1, %r8d
	cmpq	%r9, %rcx
	je	.L36
.L18:
	movl	%r8d, %edx
	andl	$127, %edx
	leal	(%rdx,%rdx), %esi
	leal	1(%rsi), %eax
	cmpl	$2, %edi
	je	.L2
	jbe	.L37
	movl	%r10d, %edx
	subl	%eax, %edx
	xorl	%eax, %eax
	sall	$3, %edx
	cmpl	$3, %edi
	cmove	%edx, %eax
	addq	$2, %rcx
	addl	$1, %r8d
	movw	%ax, -2(%rcx)
	cmpq	%r9, %rcx
	jne	.L18
.L36:
	popq	%rbx
	.cfi_remember_state
	.cfi_def_cfa_offset 48
	popq	%rbp
	.cfi_def_cfa_offset 40
	popq	%r12
	.cfi_def_cfa_offset 32
	popq	%r13
	.cfi_def_cfa_offset 24
	popq	%r14
	.cfi_def_cfa_offset 16
	popq	%r15
	.cfi_def_cfa_offset 8
	ret
	.p2align 4,,10
	.p2align 3
.L34:
	.cfi_restore_state
	andl	$127, %edx
	movzwl	0(%rbp,%rdx,2), %eax
	jmp	.L7
	.p2align 4,,10
	.p2align 3
.L2:
	cmpw	$63, %ax
	jbe	.L32
	cmpw	$191, %ax
	ja	.L17
	movl	%r10d, %edx
	subl	%eax, %edx
	movl	%edx, %eax
	sall	$4, %eax
	jmp	.L7
	.p2align 4,,10
	.p2align 3
.L17:
	leal	3841(%rsi), %eax
.L32:
	sall	$4, %eax
	jmp	.L7
	.p2align 4,,10
	.p2align 3
.L11:
	testl	%r14d, %r14d
	je	.L13
	movl	%edx, %eax
	negl	%eax
	cmovs	%edx, %eax
	cmpl	%eax, -4(%rsp)
	jb	.L13
.L14:
	movl	%edx, %eax
	negl	%eax
	sall	$11, %eax
	cltd
	idivl	%r14d
.L31:
	movl	$-1024, %edx
	cmpl	%edx, %eax
	cmovl	%edx, %eax
	movl	$1024, %edx
	cmpl	%edx, %eax
	cmovg	%edx, %eax
	jmp	.L7
	.p2align 4,,10
	.p2align 3
.L9:
	leal	1000000(%rdx), %r15d
	cmpl	$-500000, %edx
	cmovl	%r15d, %edx
	jmp	.L10
	.p2align 4,,10
	.p2align 3
.L35:
	sall	$11, %eax
	cltd
	idivl	%r11d
	jmp	.L31
.L20:
	movl	$-1024, %eax
	jmp	.L7
	.cfi_endproc
.LFE25:
	.size	make_template, .-make_template
	.p2align 4
	.type	correlation_score_ppm, @function
correlation_score_ppm:
.LFB26:
	.cfi_startproc
	pxor	%xmm2, %xmm2
	pushq	%rbp
	.cfi_def_cfa_offset 16
	.cfi_offset 6, -16
	xorl	%eax, %eax
	leaq	g_folded(%rip), %r11
	pushq	%rbx
	.cfi_def_cfa_offset 24
	.cfi_offset 3, -24
	movdqa	%xmm2, %xmm1
	leaq	g_template(%rip), %rbx
	pxor	%xmm4, %xmm4
	.p2align 4,,10
	.p2align 3
.L39:
	movdqa	(%r11,%rax), %xmm0
	movdqa	%xmm4, %xmm3
	pcmpgtw	%xmm0, %xmm3
	movdqa	%xmm0, %xmm5
	punpcklwd	%xmm3, %xmm5
	punpckhwd	%xmm3, %xmm0
	movdqa	%xmm4, %xmm3
	paddd	%xmm5, %xmm1
	paddd	%xmm0, %xmm1
	movdqa	(%rbx,%rax), %xmm0
	addq	$16, %rax
	pcmpgtw	%xmm0, %xmm3
	movdqa	%xmm0, %xmm5
	punpcklwd	%xmm3, %xmm5
	punpckhwd	%xmm3, %xmm0
	paddd	%xmm5, %xmm2
	paddd	%xmm0, %xmm2
	cmpq	$256, %rax
	jne	.L39
	movdqa	%xmm2, %xmm0
	xorl	%ecx, %ecx
	xorl	%r8d, %r8d
	xorl	%esi, %esi
	psrldq	$8, %xmm0
	xorl	%edi, %edi
	paddd	%xmm0, %xmm2
	movdqa	%xmm2, %xmm0
	psrldq	$4, %xmm0
	paddd	%xmm0, %xmm2
	movdqa	%xmm1, %xmm0
	psrldq	$8, %xmm0
	movd	%xmm2, %r10d
	paddd	%xmm0, %xmm1
	movdqa	%xmm1, %xmm0
	psrldq	$4, %xmm0
	paddd	%xmm0, %xmm1
	movd	%xmm1, %r9d
	.p2align 4,,10
	.p2align 3
.L40:
	movswl	(%r11,%rcx), %edx
	movswl	(%rbx,%rcx), %eax
	addq	$2, %rcx
	sall	$7, %edx
	sall	$7, %eax
	subl	%r9d, %edx
	subl	%r10d, %eax
	movslq	%edx, %rdx
	cltq
	movq	%rdx, %rbp
	imulq	%rdx, %rdx
	imulq	%rax, %rbp
	imulq	%rax, %rax
	addq	%rdx, %rsi
	addq	%rbp, %rdi
	addq	%rax, %r8
	cmpq	$256, %rcx
	jne	.L40
	testq	%r8, %r8
	sete	%al
	testq	%rsi, %rsi
	sete	%dl
	orb	%dl, %al
	jne	.L44
	testq	%rdi, %rdi
	jle	.L44
	movq	%rsi, %rax
	orq	%r8, %rax
	orq	%rdi, %rax
	cmpq	$1073741823, %rax
	jbe	.L42
	.p2align 4,,10
	.p2align 3
.L43:
	shrq	%rdi
	shrq	%rsi
	movq	%rdi, %rax
	shrq	%r8
	orq	%rsi, %rax
	orq	%r8, %rax
	cmpq	$1073741823, %rax
	ja	.L43
.L42:
	imulq	%rdi, %rdi
	movl	$1000000, %eax
	imulq	%r8, %rsi
	cmpq	%rsi, %rdi
	jnb	.L38
	movabsq	$4835703278458516699, %rax
	addq	$999999, %rsi
	popq	%rbx
	.cfi_remember_state
	.cfi_def_cfa_offset 16
	popq	%rbp
	.cfi_def_cfa_offset 8
	mulq	%rsi
	movq	%rdi, %rax
	movq	%rdx, %rcx
	xorl	%edx, %edx
	shrq	$18, %rcx
	divq	%rcx
	ret
.L44:
	.cfi_restore_state
	xorl	%eax, %eax
.L38:
	popq	%rbx
	.cfi_def_cfa_offset 16
	popq	%rbp
	.cfi_def_cfa_offset 8
	ret
	.cfi_endproc
.LFE26:
	.size	correlation_score_ppm, .-correlation_score_ppm
	.p2align 4
	.type	classify_fold, @function
classify_fold:
.LFB27:
	.cfi_startproc
	pushq	%r15
	.cfi_def_cfa_offset 16
	.cfi_offset 15, -16
	xorl	%ecx, %ecx
	xorl	%edx, %edx
	pxor	%xmm0, %xmm0
	pushq	%r14
	.cfi_def_cfa_offset 24
	.cfi_offset 14, -24
	xorl	%r15d, %r15d
	pushq	%r13
	.cfi_def_cfa_offset 32
	.cfi_offset 13, -32
	movq	%rdi, %r13
	pushq	%r12
	.cfi_def_cfa_offset 40
	.cfi_offset 12, -40
	movq	%r15, %r12
	pushq	%rbp
	.cfi_def_cfa_offset 48
	.cfi_offset 6, -48
	movl	%esi, %ebp
	pushq	%rbx
	.cfi_def_cfa_offset 56
	.cfi_offset 3, -56
	subq	$88, %rsp
	.cfi_def_cfa_offset 144
	movq	%fs:40, %rax
	movq	%rax, 72(%rsp)
	xorl	%eax, %eax
	movl	%edx, 8(%rsp)
	leaq	48(%rsp), %r14
	movq	$0, 40(%rsp)
	movl	%ecx, 12(%rsp)
	movaps	%xmm0, 48(%rsp)
.L60:
	movl	%r12d, %ebx
	xorl	%r15d, %r15d
	cmpq	$1, %r12
	je	.L79
	.p2align 4,,10
	.p2align 3
.L51:
	xorl	%ecx, %ecx
	xorl	%edx, %edx
	movl	%r15d, %r8d
	movl	%ebp, %esi
	movl	%ebx, %edi
	call	make_template
	call	correlation_score_ppm
	cmpl	%eax, (%r14,%r12,4)
	jnb	.L58
	movl	%eax, (%r14,%r12,4)
	movw	%r15w, 40(%rsp,%r12,2)
.L58:
	addl	$1, %r15d
	cmpl	$128, %r15d
	jne	.L51
	addq	$1, %r12
	cmpq	$4, %r12
	jne	.L60
	movl	8(%rsp), %edx
	movl	12(%rsp), %ecx
	movq	%r13, %r12
	xorl	%eax, %eax
	xorl	%ebx, %ebx
	xorl	%r13d, %r13d
	movl	$4, %edi
.L59:
	movl	(%r14,%rax,4), %esi
	cmpl	%esi, %ebx
	cmovb	%esi, %ebx
	cmpl	%esi, %r13d
	jnb	.L62
	movl	%r13d, %ebx
	movl	%eax, %edi
	movl	%esi, %r13d
.L62:
	addq	$1, %rax
	cmpq	$4, %rax
	jne	.L59
	cmpl	$4, %edi
	je	.L69
	movl	%edi, %eax
	movl	%ebp, %esi
	movzwl	40(%rsp,%rax,2), %r8d
	xorl	%eax, %eax
	cmpl	$1, %edi
	cmovne	%eax, %ecx
	cmovne	%eax, %edx
	movl	%r8d, %r14d
	movl	%ecx, 12(%rsp)
	movl	%edx, 8(%rsp)
	call	make_template
	movl	8(%rsp), %edx
	movl	12(%rsp), %ecx
.L63:
	movd	%ebx, %xmm1
	movd	%ebp, %xmm2
	movd	%edi, %xmm0
	movl	%edx, 16(%r12)
	movd	%r13d, %xmm3
	punpckldq	%xmm2, %xmm1
	movl	%ecx, 20(%r12)
	punpckldq	%xmm3, %xmm0
	movw	%r14w, 24(%r12)
	punpcklqdq	%xmm1, %xmm0
	movups	%xmm0, (%r12)
	movq	72(%rsp), %rax
	subq	%fs:40, %rax
	jne	.L80
	addq	$88, %rsp
	.cfi_remember_state
	.cfi_def_cfa_offset 56
	movq	%r12, %rax
	popq	%rbx
	.cfi_def_cfa_offset 48
	popq	%rbp
	.cfi_def_cfa_offset 40
	popq	%r12
	.cfi_def_cfa_offset 32
	popq	%r13
	.cfi_def_cfa_offset 24
	popq	%r14
	.cfi_def_cfa_offset 16
	popq	%r15
	.cfi_def_cfa_offset 8
	ret
.L79:
	.cfi_restore_state
	movq	%r13, 16(%rsp)
	leaq	edge_options.2(%rip), %r15
	movq	%r14, 24(%rsp)
	movq	%r15, %r12
.L52:
	leaq	edge_options.2(%rip), %r13
	movq	%r13, %rbx
	.p2align 4,,10
	.p2align 3
.L57:
	xorl	%r15d, %r15d
	.p2align 4,,10
	.p2align 3
.L54:
	movl	(%rbx), %r14d
	movl	(%r12), %r13d
	movl	%r15d, %r8d
	movl	%ebp, %esi
	movl	$1, %edi
	movl	%r14d, %ecx
	movl	%r13d, %edx
	call	make_template
	call	correlation_score_ppm
	cmpl	%eax, 52(%rsp)
	jnb	.L53
	movl	%eax, 52(%rsp)
	movw	%r15w, 42(%rsp)
	movl	%r14d, 12(%rsp)
	movl	%r13d, 8(%rsp)
.L53:
	addl	$1, %r15d
	cmpl	$128, %r15d
	jne	.L54
	addq	$4, %rbx
	leaq	20+edge_options.2(%rip), %rax
	cmpq	%rax, %rbx
	jne	.L57
	addq	$4, %r12
	cmpq	%rbx, %r12
	jne	.L52
	movq	16(%rsp), %r13
	movq	24(%rsp), %r14
	movl	$2, %r12d
	jmp	.L60
.L69:
	xorl	%edx, %edx
	xorl	%ecx, %ecx
	xorl	%r14d, %r14d
	jmp	.L63
.L80:
	call	__stack_chk_fail@PLT
	.cfi_endproc
.LFE27:
	.size	classify_fold, .-classify_fold
	.p2align 4
	.type	fast_shape_score, @function
fast_shape_score:
.LFB28:
	.cfi_startproc
	leaq	g_template(%rip), %rdx
	pushq	%r12
	.cfi_def_cfa_offset 16
	.cfi_offset 12, -16
	movq	%rdi, %r11
	pxor	%xmm0, %xmm0
	pushq	%rbp
	.cfi_def_cfa_offset 24
	.cfi_offset 6, -24
	movq	%rdx, %r9
	movq	%rdx, %rax
	pxor	%xmm3, %xmm3
	pushq	%rbx
	.cfi_def_cfa_offset 32
	.cfi_offset 3, -32
	movq	%rsi, %rbx
	leaq	256(%rdx), %rsi
.L82:
	movdqa	(%rax), %xmm1
	movdqa	%xmm3, %xmm2
	addq	$16, %rax
	pcmpgtw	%xmm1, %xmm2
	movdqa	%xmm1, %xmm4
	punpcklwd	%xmm2, %xmm4
	punpckhwd	%xmm2, %xmm1
	paddd	%xmm4, %xmm0
	paddd	%xmm1, %xmm0
	cmpq	%rax, %rsi
	jne	.L82
	movdqa	%xmm0, %xmm1
	xorl	%r10d, %r10d
	psrldq	$8, %xmm1
	paddd	%xmm1, %xmm0
	movdqa	%xmm0, %xmm1
	psrldq	$4, %xmm1
	paddd	%xmm1, %xmm0
	movd	%xmm0, %ecx
.L83:
	movswl	(%rdx), %eax
	addq	$2, %rdx
	sall	$7, %eax
	subl	%ecx, %eax
	cltq
	imulq	%rax, %rax
	addq	%rax, %r10
	cmpq	%rsi, %rdx
	jne	.L83
	xorl	%eax, %eax
	xorl	%ebp, %ebp
	leaq	512+g_centered_fold(%rip), %r8
	xorl	%r12d, %r12d
	movw	%ax, (%rbx)
	.p2align 4,,10
	.p2align 3
.L84:
	leaq	g_centered_fold(%rip), %rax
	movl	%ebp, %ecx
	xorl	%esi, %esi
	.p2align 4,,10
	.p2align 3
.L85:
	movq	%rcx, %rdx
	movslq	(%rax), %rdi
	addq	$4, %rax
	addl	$1, %ecx
	andl	$127, %edx
	movswq	(%r9,%rdx,2), %rdx
	imulq	%rdi, %rdx
	addq	%rdx, %rsi
	cmpq	%r8, %rax
	jne	.L85
	cmpq	%r12, %rsi
	jle	.L86
	movw	%bp, (%rbx)
	movq	%rsi, %r12
.L86:
	addl	$1, %ebp
	cmpw	$128, %bp
	jne	.L84
	testq	%r11, %r11
	sete	%al
	testq	%r10, %r10
	sete	%dl
	orb	%dl, %al
	jne	.L91
	testq	%r12, %r12
	jle	.L91
	movq	%r11, %rax
	salq	$7, %r12
	orq	%r10, %rax
	orq	%r12, %rax
	cmpq	$1073741823, %rax
	jbe	.L89
.L90:
	shrq	%r12
	shrq	%r11
	movq	%r12, %rax
	shrq	%r10
	orq	%r11, %rax
	orq	%r10, %rax
	cmpq	$1073741823, %rax
	ja	.L90
.L89:
	imulq	%r12, %r12
	movl	$1000000, %eax
	imulq	%r10, %r11
	cmpq	%r11, %r12
	jnb	.L81
	movabsq	$4835703278458516699, %rax
	addq	$999999, %r11
	popq	%rbx
	.cfi_remember_state
	.cfi_def_cfa_offset 24
	popq	%rbp
	.cfi_def_cfa_offset 16
	mulq	%r11
	movq	%r12, %rax
	popq	%r12
	.cfi_def_cfa_offset 8
	movq	%rdx, %rcx
	xorl	%edx, %edx
	shrq	$18, %rcx
	divq	%rcx
	ret
.L91:
	.cfi_restore_state
	xorl	%eax, %eax
.L81:
	popq	%rbx
	.cfi_def_cfa_offset 24
	popq	%rbp
	.cfi_def_cfa_offset 16
	popq	%r12
	.cfi_def_cfa_offset 8
	ret
	.cfi_endproc
.LFE28:
	.size	fast_shape_score, .-fast_shape_score
	.p2align 4
	.type	classify_fold_fast, @function
classify_fold_fast:
.LFB29:
	.cfi_startproc
	pushq	%r15
	.cfi_def_cfa_offset 16
	.cfi_offset 15, -16
	pxor	%xmm0, %xmm0
	pxor	%xmm4, %xmm4
	pushq	%r14
	.cfi_def_cfa_offset 24
	.cfi_offset 14, -24
	pushq	%r13
	.cfi_def_cfa_offset 32
	.cfi_offset 13, -32
	pushq	%r12
	.cfi_def_cfa_offset 40
	.cfi_offset 12, -40
	pushq	%rbp
	.cfi_def_cfa_offset 48
	.cfi_offset 6, -48
	movq	%rdi, %rbp
	pushq	%rbx
	.cfi_def_cfa_offset 56
	.cfi_offset 3, -56
	subq	$104, %rsp
	.cfi_def_cfa_offset 160
	movl	%esi, 12(%rsp)
	leaq	g_folded(%rip), %rsi
	leaq	256(%rsi), %rdx
	movq	%fs:40, %rax
	movq	%rax, 88(%rsp)
	xorl	%eax, %eax
	movaps	%xmm0, 64(%rsp)
	movq	%rsi, %rax
	movq	$0, 56(%rsp)
	.p2align 4,,10
	.p2align 3
.L99:
	movdqa	(%rax), %xmm2
	movdqa	%xmm4, %xmm3
	addq	$16, %rax
	pcmpgtw	%xmm2, %xmm3
	movdqa	%xmm2, %xmm1
	punpcklwd	%xmm3, %xmm1
	punpckhwd	%xmm3, %xmm2
	paddd	%xmm1, %xmm0
	paddd	%xmm2, %xmm0
	cmpq	%rdx, %rax
	jne	.L99
	movdqa	%xmm0, %xmm1
	xorl	%edx, %edx
	leaq	g_centered_fold(%rip), %rdi
	xorl	%r15d, %r15d
	psrldq	$8, %xmm1
	paddd	%xmm1, %xmm0
	movdqa	%xmm0, %xmm1
	psrldq	$4, %xmm1
	paddd	%xmm1, %xmm0
	movd	%xmm0, %ecx
	.p2align 4,,10
	.p2align 3
.L100:
	movswl	(%rsi,%rdx,2), %eax
	sall	$7, %eax
	subl	%ecx, %eax
	movl	%eax, (%rdi,%rdx,4)
	cltq
	addq	$1, %rdx
	imulq	%rax, %rax
	addq	%rax, %r15
	cmpq	$128, %rdx
	jne	.L100
	leaq	56(%rsp), %rax
	movl	$0, 20(%rsp)
	leaq	64(%rsp), %rbx
	xorl	%r12d, %r12d
	movl	$0, 16(%rsp)
	movq	%rbx, %r13
	movq	%rax, 24(%rsp)
.L107:
	movl	%r12d, %edi
	cmpq	$1, %r12
	je	.L124
	movl	12(%rsp), %esi
	xorl	%r8d, %r8d
	xorl	%ecx, %ecx
	xorl	%edx, %edx
	call	make_template
	movq	24(%rsp), %rax
	movq	%r15, %rdi
	leaq	(%rax,%r12,2), %rsi
	call	fast_shape_score
	movl	%eax, 0(%r13,%r12,4)
	addq	$1, %r12
	cmpq	$4, %r12
	jne	.L107
	movq	%r13, %rbx
	xorl	%eax, %eax
	xorl	%ecx, %ecx
	xorl	%esi, %esi
	movl	$4, %edi
.L106:
	movl	(%rbx,%rax,4), %edx
	cmpl	%edx, %ecx
	cmovb	%edx, %ecx
	cmpl	%edx, %esi
	jnb	.L109
	movl	%esi, %ecx
	movl	%eax, %edi
	movl	%edx, %esi
.L109:
	addq	$1, %rax
	cmpq	$4, %rax
	jne	.L106
	movd	12(%rsp), %xmm5
	movd	%ecx, %xmm1
	movd	%edi, %xmm0
	movd	%esi, %xmm6
	punpckldq	%xmm6, %xmm0
	punpckldq	%xmm5, %xmm1
	punpcklqdq	%xmm1, %xmm0
	cmpl	$4, %edi
	je	.L115
	movl	20(%rsp), %ecx
	movl	%edi, %eax
	movd	%xmm5, %esi
	movzwl	56(%rsp,%rax,2), %r8d
	xorl	%eax, %eax
	cmpl	$1, %edi
	cmovne	%eax, %ecx
	cmove	16(%rsp), %eax
	movl	%r8d, %r12d
	movl	%eax, %edx
	movl	%ecx, 20(%rsp)
	movl	%eax, 16(%rsp)
	call	make_template
.L110:
	movl	16(%rsp), %eax
	movw	%r12w, 24(%rbp)
	movups	%xmm0, 0(%rbp)
	movl	%eax, 16(%rbp)
	movl	20(%rsp), %eax
	movl	%eax, 20(%rbp)
	movq	88(%rsp), %rax
	subq	%fs:40, %rax
	jne	.L125
	addq	$104, %rsp
	.cfi_remember_state
	.cfi_def_cfa_offset 56
	movq	%rbp, %rax
	popq	%rbx
	.cfi_def_cfa_offset 48
	popq	%rbp
	.cfi_def_cfa_offset 40
	popq	%r12
	.cfi_def_cfa_offset 32
	popq	%r13
	.cfi_def_cfa_offset 24
	popq	%r14
	.cfi_def_cfa_offset 16
	popq	%r15
	.cfi_def_cfa_offset 8
	ret
.L124:
	.cfi_restore_state
	movq	%rbp, 32(%rsp)
	leaq	edges.3(%rip), %rbx
	leaq	54(%rsp), %r14
	movq	%r13, 40(%rsp)
	.p2align 4,,10
	.p2align 3
.L102:
	leaq	edges.3(%rip), %r12
	.p2align 4,,10
	.p2align 3
.L104:
	movl	(%r12), %r13d
	movl	(%rbx), %ebp
	xorl	%r8d, %r8d
	movl	$1, %edi
	movl	12(%rsp), %esi
	movl	%r13d, %ecx
	movl	%ebp, %edx
	call	make_template
	movq	%r14, %rsi
	movq	%r15, %rdi
	call	fast_shape_score
	cmpl	%eax, 68(%rsp)
	jnb	.L103
	movl	%eax, 68(%rsp)
	movzwl	54(%rsp), %eax
	movl	%r13d, 20(%rsp)
	movw	%ax, 58(%rsp)
	movl	%ebp, 16(%rsp)
.L103:
	addq	$4, %r12
	leaq	20+edges.3(%rip), %rax
	cmpq	%r12, %rax
	jne	.L104
	addq	$4, %rbx
	cmpq	%rbx, %rax
	jne	.L102
	movq	32(%rsp), %rbp
	movq	40(%rsp), %r13
	movl	$2, %r12d
	jmp	.L107
.L115:
	movl	$0, 20(%rsp)
	xorl	%r12d, %r12d
	movl	$0, 16(%rsp)
	jmp	.L110
.L125:
	call	__stack_chk_fail@PLT
	.cfi_endproc
.LFE29:
	.size	classify_fold_fast, .-classify_fold_fast
	.p2align 4
	.type	fold_cycles, @function
fold_cycles:
.LFB19:
	.cfi_startproc
	pushq	%r15
	.cfi_def_cfa_offset 16
	.cfi_offset 15, -16
	movl	$64, %ecx
	pcmpeqd	%xmm0, %xmm0
	movl	%esi, %r10d
	pushq	%r14
	.cfi_def_cfa_offset 24
	.cfi_offset 14, -24
	pushq	%r13
	.cfi_def_cfa_offset 32
	.cfi_offset 13, -32
	pushq	%r12
	.cfi_def_cfa_offset 40
	.cfi_offset 12, -40
	pushq	%rbp
	.cfi_def_cfa_offset 48
	.cfi_offset 6, -48
	movq	%rdi, %rbp
	pushq	%rbx
	.cfi_def_cfa_offset 56
	.cfi_offset 3, -56
	movzwl	8(%rdi), %eax
	leaq	g_fold_accumulator(%rip), %rdi
	movaps	%xmm0, g_fold_minimum(%rip)
	subl	$1, %eax
	movaps	%xmm0, 16+g_fold_minimum(%rip)
	sall	$12, %eax
	movl	%esi, -12(%rsp)
	movl	%eax, -20(%rsp)
	movl	%eax, %ebx
	xorl	%eax, %eax
	rep stosq
	movl	$32, %ecx
	movaps	%xmm0, 32+g_fold_minimum(%rip)
	movaps	%xmm0, 48+g_fold_minimum(%rip)
	movaps	%xmm0, 64+g_fold_minimum(%rip)
	movaps	%xmm0, 80+g_fold_minimum(%rip)
	movaps	%xmm0, 96+g_fold_minimum(%rip)
	movaps	%xmm0, 112+g_fold_minimum(%rip)
	movq	%rdi, %r12
	leaq	g_fold_maximum(%rip), %rdi
	movaps	%xmm0, 128+g_fold_minimum(%rip)
	movaps	%xmm0, 144+g_fold_minimum(%rip)
	movaps	%xmm0, 160+g_fold_minimum(%rip)
	movaps	%xmm0, 176+g_fold_minimum(%rip)
	movaps	%xmm0, 192+g_fold_minimum(%rip)
	movaps	%xmm0, 208+g_fold_minimum(%rip)
	movaps	%xmm0, 224+g_fold_minimum(%rip)
	movaps	%xmm0, 240+g_fold_minimum(%rip)
	rep stosq
	movq	%r10, %rax
	salq	$8, %rax
	subq	%r10, %rax
	shrq	$8, %rax
	movl	%eax, -16(%rsp)
	cmpl	%ebx, %eax
	jnb	.L141
	movzwl	8(%rbp), %r13d
	movq	0(%rbp), %r11
	movq	%r10, -8(%rsp)
	xorl	%edx, %edx
	leaq	(%r10,%r10), %r14
	xorl	%ebx, %ebx
	leal	-1(%r13), %eax
	movq	%rax, %r13
	leaq	(%r11,%rax,2), %r15
	xorl	%eax, %eax
	movl	%eax, %r10d
.L136:
	movw	%r10w, -22(%rsp)
	movq	-8(%rsp), %r9
	leaq	g_fold_accumulator(%rip), %rsi
	leaq	g_fold_maximum(%rip), %r8
	leaq	g_fold_minimum(%rip), %rdi
	jmp	.L134
	.p2align 4,,10
	.p2align 3
.L128:
	movl	%eax, %r10d
	addl	$1, %eax
	andl	$4095, %ecx
	movzwl	(%r11,%r10,2), %r10d
	movzwl	(%r11,%rax,2), %eax
	subl	%r10d, %eax
	imull	%ecx, %eax
	sarl	$12, %eax
	addl	%r10d, %eax
	js	.L144
	movzwl	10(%rbp), %ecx
	cmpl	%eax, %ecx
	cmovle	%ecx, %eax
	movl	%eax, %ecx
	addl	%edx, %eax
.L129:
	movl	%eax, (%rsi)
	cmpw	(%rdi), %cx
	jnb	.L132
	movw	%cx, (%rdi)
.L132:
	cmpw	%cx, (%r8)
	jnb	.L131
	movw	%cx, (%r8)
.L131:
	addq	$4, %rsi
	addq	%r14, %r9
	addq	$2, %r8
	addq	$2, %rdi
	cmpq	%rsi, %r12
	je	.L133
	movl	(%rsi), %edx
.L134:
	movq	%r9, %rcx
	shrq	$8, %rcx
	addl	%ebx, %ecx
	movl	%ecx, %eax
	shrl	$12, %eax
	cmpl	%r13d, %eax
	jb	.L128
	movzwl	(%r15), %eax
	movl	%eax, %ecx
	addl	%edx, %eax
	jmp	.L129
	.p2align 4,,10
	.p2align 3
.L144:
	cmpw	$0, (%rdi)
	movl	%edx, (%rsi)
	je	.L131
	xorl	%eax, %eax
	movw	%ax, (%rdi)
	jmp	.L131
.L133:
	movl	-12(%rsp), %eax
	movzwl	-22(%rsp), %r10d
	addl	%eax, %ebx
	movl	-16(%rsp), %eax
	leal	1(%r10), %ecx
	leal	(%rbx,%rax), %edx
	movl	-20(%rsp), %eax
	cmpl	%eax, %edx
	jnb	.L135
	leaq	g_fold_accumulator(%rip), %rax
	movl	%ecx, %r10d
	movl	(%rax), %edx
	jmp	.L136
.L135:
	movl	%r10d, %eax
	cmpw	$2, %cx
	jbe	.L126
	cmpw	$4, %cx
	leal	-1(%rax), %edi
	leaq	g_folded(%rip), %r9
	cmovbe	%ecx, %edi
	xorl	%esi, %esi
	movl	%edi, %r8d
	movzwl	%di, %edi
	shrw	%r8w
	movzwl	%r8w, %r8d
	.p2align 4,,10
	.p2align 3
.L139:
	leaq	g_fold_accumulator(%rip), %rax
	movl	(%rax,%rsi,2), %eax
	cmpw	$4, %cx
	jbe	.L138
	leaq	g_fold_minimum(%rip), %rbx
	movzwl	(%rbx,%rsi), %edx
	leaq	g_fold_maximum(%rip), %rbx
	subl	%edx, %eax
	movzwl	(%rbx,%rsi), %edx
	subl	%edx, %eax
.L138:
	addl	%r8d, %eax
	xorl	%edx, %edx
	divl	%edi
	movw	%ax, (%r9,%rsi)
	addq	$2, %rsi
	cmpq	$256, %rsi
	jne	.L139
.L126:
	popq	%rbx
	.cfi_remember_state
	.cfi_def_cfa_offset 48
	movl	%ecx, %eax
	popq	%rbp
	.cfi_def_cfa_offset 40
	popq	%r12
	.cfi_def_cfa_offset 32
	popq	%r13
	.cfi_def_cfa_offset 24
	popq	%r14
	.cfi_def_cfa_offset 16
	popq	%r15
	.cfi_def_cfa_offset 8
	ret
.L141:
	.cfi_restore_state
	xorl	%ecx, %ecx
	popq	%rbx
	.cfi_def_cfa_offset 48
	popq	%rbp
	.cfi_def_cfa_offset 40
	movl	%ecx, %eax
	popq	%r12
	.cfi_def_cfa_offset 32
	popq	%r13
	.cfi_def_cfa_offset 24
	popq	%r14
	.cfi_def_cfa_offset 16
	popq	%r15
	.cfi_def_cfa_offset 8
	ret
	.cfi_endproc
.LFE19:
	.size	fold_cycles, .-fold_cycles
	.p2align 4
	.type	append_period_candidate, @function
append_period_candidate:
.LFB32:
	.cfi_startproc
	movq	%rsi, %r8
	movl	%edx, %esi
	cmpl	$8191, %edx
	jbe	.L145
	movl	12(%rcx), %eax
	shrl	%edx
	movl	%esi, %ecx
	salq	$12, %rax
	addq	%rdx, %rax
	xorl	%edx, %edx
	divq	%rcx
	subl	$98000, %eax
	cmpl	$304000, %eax
	ja	.L145
	movzwl	(%r8), %r11d
	testw	%r11w, %r11w
	je	.L151
	movl	%esi, %r9d
	movzwl	%r11w, %edx
	movq	%rdi, %rax
	shrl	$8, %r9d
	leaq	(%rdi,%rdx,4), %r10
	addl	$1, %r9d
	jmp	.L150
	.p2align 4,,10
	.p2align 3
.L154:
	addq	$4, %rax
	cmpq	%r10, %rax
	je	.L153
.L150:
	movl	(%rax), %ecx
	movl	%esi, %edx
	movl	%ecx, %edi
	subl	%ecx, %edx
	subl	%esi, %edi
	cmpl	%esi, %ecx
	cmovnb	%edi, %edx
	cmpl	%edx, %r9d
	jb	.L154
.L145:
	ret
	.p2align 4,,10
	.p2align 3
.L153:
	cmpw	$11, %r11w
	ja	.L145
	addl	$1, %r11d
	movq	%rax, %rdi
.L147:
	movw	%r11w, (%r8)
	movl	%esi, (%rdi)
	ret
.L151:
	movl	$1, %r11d
	jmp	.L147
	.cfi_endproc
.LFE32:
	.size	append_period_candidate, .-append_period_candidate
	.p2align 4
	.type	estimate_duty_ppm, @function
estimate_duty_ppm:
.LFB22:
	.cfi_startproc
	pushq	%r14
	.cfi_def_cfa_offset 16
	.cfi_offset 14, -16
	leaq	2+g_sort_buffer(%rip), %r10
	movl	$1, %r9d
	pushq	%r13
	.cfi_def_cfa_offset 24
	.cfi_offset 13, -24
	leaq	-2(%r10), %r8
	pushq	%r12
	.cfi_def_cfa_offset 32
	.cfi_offset 12, -32
	pushq	%rbp
	.cfi_def_cfa_offset 40
	.cfi_offset 6, -40
	pushq	%rbx
	.cfi_def_cfa_offset 48
	.cfi_offset 3, -48
	subq	$80, %rsp
	.cfi_def_cfa_offset 128
	movdqa	g_folded(%rip), %xmm6
	movdqa	16+g_folded(%rip), %xmm7
	movq	%fs:40, %rax
	movq	%rax, 72(%rsp)
	xorl	%eax, %eax
	movdqa	96+g_folded(%rip), %xmm4
	movaps	%xmm6, g_sort_buffer(%rip)
	movdqa	32+g_folded(%rip), %xmm6
	movaps	%xmm7, 16+g_sort_buffer(%rip)
	movdqa	48+g_folded(%rip), %xmm7
	movaps	%xmm6, 32+g_sort_buffer(%rip)
	movdqa	64+g_folded(%rip), %xmm6
	movaps	%xmm7, 48+g_sort_buffer(%rip)
	movdqa	80+g_folded(%rip), %xmm7
	movaps	%xmm6, 64+g_sort_buffer(%rip)
	movdqa	112+g_folded(%rip), %xmm6
	movaps	%xmm7, 80+g_sort_buffer(%rip)
	movdqa	128+g_folded(%rip), %xmm7
	movaps	%xmm6, 112+g_sort_buffer(%rip)
	movaps	%xmm7, 128+g_sort_buffer(%rip)
	movaps	%xmm4, 96+g_sort_buffer(%rip)
	movdqa	144+g_folded(%rip), %xmm4
	movdqa	160+g_folded(%rip), %xmm6
	movdqa	176+g_folded(%rip), %xmm7
	movdqa	240+g_folded(%rip), %xmm1
	movaps	%xmm4, 144+g_sort_buffer(%rip)
	movdqa	192+g_folded(%rip), %xmm4
	movaps	%xmm6, 160+g_sort_buffer(%rip)
	movdqa	208+g_folded(%rip), %xmm6
	movaps	%xmm7, 176+g_sort_buffer(%rip)
	movdqa	224+g_folded(%rip), %xmm7
	movaps	%xmm4, 192+g_sort_buffer(%rip)
	movaps	%xmm6, 208+g_sort_buffer(%rip)
	movaps	%xmm7, 224+g_sort_buffer(%rip)
	movaps	%xmm1, 240+g_sort_buffer(%rip)
	.p2align 4,,10
	.p2align 3
.L159:
	movzwl	(%r10), %edi
	movl	%r9d, %eax
	movq	%r10, %rdx
	jmp	.L156
	.p2align 4,,10
	.p2align 3
.L158:
	movw	%cx, (%rdx)
	subq	$2, %rdx
	testl	%eax, %eax
	je	.L179
.L156:
	leal	-1(%rax), %ecx
	movslq	%eax, %rsi
	movq	%rcx, %rax
	movzwl	(%r8,%rcx,2), %ecx
	cmpw	%cx, %di
	jl	.L158
.L157:
	addq	$1, %r9
	movw	%di, (%r8,%rsi,2)
	addq	$2, %r10
	cmpq	$128, %r9
	jne	.L159
	pxor	%xmm1, %xmm1
	xorl	%eax, %eax
	leaq	248+g_sort_buffer(%rip), %rcx
	movdqa	%xmm1, %xmm2
	pxor	%xmm4, %xmm4
.L160:
	movq	(%r8,%rax), %xmm0
	movdqa	%xmm4, %xmm3
	movq	%rax, %rdx
	addq	$8, %rax
	negq	%rdx
	pcmpgtw	%xmm0, %xmm3
	movdqa	%xmm0, %xmm5
	punpcklwd	%xmm3, %xmm5
	punpcklwd	%xmm3, %xmm0
	movdqa	%xmm4, %xmm3
	paddd	%xmm5, %xmm2
	pshufd	$78, %xmm0, %xmm0
	paddd	%xmm0, %xmm2
	movq	(%rcx,%rdx), %xmm0
	pshuflw	$27, %xmm0, %xmm0
	pcmpgtw	%xmm0, %xmm3
	movdqa	%xmm0, %xmm5
	punpcklwd	%xmm3, %xmm5
	punpcklwd	%xmm3, %xmm0
	paddd	%xmm5, %xmm1
	pshufd	$78, %xmm0, %xmm0
	paddd	%xmm0, %xmm1
	cmpq	$16, %rax
	jne	.L160
	movd	%xmm2, %edx
	pshufd	$0xe5, %xmm2, %xmm2
	movd	%xmm2, %eax
	movd	%xmm1, %ecx
	addl	%eax, %edx
	pshufd	$0xe5, %xmm1, %xmm4
	leaq	g_folded(%rip), %rbx
	movl	$2048, %r9d
	leal	7(%rdx), %eax
	movq	%rbx, %rdi
	cmovns	%edx, %eax
	sarl	$3, %eax
	movl	%eax, %edx
	movd	%xmm4, %eax
	addl	%eax, %ecx
	leal	7(%rcx), %eax
	cmovns	%ecx, %eax
	xorl	%ecx, %ecx
	xorl	%r8d, %r8d
	xorl	%esi, %esi
	xorl	%r11d, %r11d
	sarl	$3, %eax
	addl	%edx, %eax
	movl	%eax, %r10d
	shrl	$31, %r10d
	addl	%eax, %r10d
	sarl	%r10d
	movswl	%r10w, %r12d
	jmp	.L169
	.p2align 4,,10
	.p2align 3
.L199:
	cmpw	%ax, %r10w
	jge	.L164
	cmpw	$7, %r11w
	jbe	.L197
	.p2align 4,,10
	.p2align 3
.L164:
	addq	$2, %rdi
	addl	$4096, %r9d
	cmpw	$128, %cx
	je	.L198
.L169:
	addl	$1, %ecx
	movswl	(%rdi), %edx
	movq	%rcx, %rax
	andl	$127, %eax
	movswl	(%rbx,%rax,2), %eax
	cmpw	%r10w, %dx
	jle	.L199
	addl	$1, %r8d
	cmpw	%ax, %r10w
	jl	.L164
	cmpw	$7, %si
	ja	.L164
	movswl	%dx, %r14d
	xorl	%edx, %edx
	leal	1(%rsi), %ebp
	movzwl	%si, %r13d
	subl	%r14d, %eax
	movl	%eax, %esi
	je	.L168
	movl	%r12d, %eax
	subl	%r14d, %eax
	sall	$12, %eax
	cltd
	idivl	%esi
	movl	$4095, %edx
	cmpl	%edx, %eax
	cmovle	%eax, %edx
	xorl	%eax, %eax
	testl	%edx, %edx
	cmovs	%eax, %edx
.L168:
	addl	%r9d, %edx
	movslq	%r13d, %rsi
	andl	$524287, %edx
	movl	%edx, 32(%rsp,%rsi,4)
	movl	%ebp, %esi
	jmp	.L164
	.p2align 4,,10
	.p2align 3
.L179:
	xorl	%esi, %esi
	jmp	.L157
	.p2align 4,,10
	.p2align 3
.L197:
	movswl	%ax, %r14d
	leal	1(%r11), %r13d
	xorl	%ebp, %ebp
	subl	%edx, %r14d
	je	.L167
	movl	%r12d, %eax
	subl	%edx, %eax
	sall	$12, %eax
	cltd
	idivl	%r14d
	movl	$4095, %edx
	cmpl	%edx, %eax
	cmovle	%eax, %edx
	xorl	%eax, %eax
	movl	%edx, %ebp
	testl	%edx, %edx
	cmovs	%eax, %ebp
.L167:
	addl	%r9d, %ebp
	movslq	%r11d, %r11
	andl	$524287, %ebp
	movl	%ebp, (%rsp,%r11