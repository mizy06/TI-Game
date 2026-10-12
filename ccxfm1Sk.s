	.file	"waveform_cascade.c"
	.text
	.p2align 4
	.type	sin_q10, @function
sin_q10:
.LFB7:
	.cfi_startproc
	movl	%edi, %esi
	movl	%edi, %eax
	shrl	$17, %eax
	shrl	$25, %esi
	andl	$16777216, %edi
	movzbl	%al, %eax
	je	.L2
	leal	1(%rsi), %ecx
	addl	$-128, %eax
	movl	%esi, %edx
	andl	$127, %ecx
.L3:
	leaq	sine128(%rip), %rdi
	movl	$256, %esi
	movswl	(%rdi,%rdx,2), %edx
	movswl	(%rdi,%rcx,2), %ecx
	subl	%eax, %esi
	imull	%esi, %edx
	imull	%ecx, %eax
	addl	%eax, %edx
	leal	255(%rdx), %eax
	cmovns	%edx, %eax
	sarl	$8, %eax
	ret
	.p2align 4,,10
	.p2align 3
.L2:
	leal	127(%rsi), %edx
	subl	$-128, %eax
	movl	%esi, %ecx
	andl	$127, %edx
	jmp	.L3
	.cfi_endproc
.LFE7:
	.size	sin_q10, .-sin_q10
	.p2align 4
	.type	check_frame, @function
check_frame:
.LFB11:
	.cfi_startproc
	testq	%rdi, %rdi
	je	.L28
	movq	(%rdi), %rax
	testq	%rax, %rax
	je	.L28
	movzwl	8(%rdi), %edx
	leal	-1024(%rdx), %ecx
	cmpw	$3072, %cx
	ja	.L33
	movzwl	10(%rdi), %r8d
	leal	-255(%r8), %ecx
	cmpw	$16128, %cx
	ja	.L30
	pushq	%rbp
	.cfi_def_cfa_offset 16
	.cfi_offset 6, -16
	pushq	%rbx
	.cfi_def_cfa_offset 24
	.cfi_offset 3, -24
	movl	12(%rdi), %ebx
	leal	-10000000(%rbx), %ecx
	cmpl	$40000000, %ecx
	jbe	.L34
.L9:
	movl	$9, 4(%rsi)
.L6:
	xorl	%eax, %eax
.L5:
	popq	%rbx
	.cfi_def_cfa_offset 16
	popq	%rbp
	.cfi_def_cfa_offset 8
	ret
	.p2align 4,,10
	.p2align 3
.L33:
	.cfi_restore 3
	.cfi_restore 6
	movl	$10, 4(%rsi)
.L28:
	xorl	%eax, %eax
	ret
	.p2align 4,,10
	.p2align 3
.L34:
	.cfi_def_cfa_offset 24
	.cfi_offset 3, -24
	.cfi_offset 6, -16
	movl	16(%rdi), %ebx
	leal	-1000000(%rbx), %ecx
	cmpl	$4500000, %ecx
	ja	.L9
	movl	20(%rdi), %ebx
	leal	-100000(%rbx), %ecx
	cmpl	$9900000, %ecx
	ja	.L9
	movl	28(%rdi), %ecx
	testb	$1, %cl
	je	.L9
	testb	$2, %cl
	jne	.L11
	orw	$1, 60(%rsi)
.L11:
	andl	$4, %ecx
	jne	.L12
	orw	$2, 60(%rsi)
.L12:
	leal	-1(%rdx), %ecx
	movzwl	32(%rsi), %r9d
	xorl	%r11d, %r11d
	xorl	%r10d, %r10d
	leaq	2(%rax,%rcx,2), %rbx
	movzwl	%r8w, %ebp
	jmp	.L16
	.p2align 4,,10
	.p2align 3
.L36:
	addl	$1, %r9d
	movw	%r9w, 32(%rsi)
.L14:
	addl	$3, %edi
	cmpl	%ebp, %edi
	sbbl	$-1, %r11d
.L15:
	addq	$2, %rax
	cmpq	%rax, %rbx
	je	.L35
.L16:
	movzwl	(%rax), %edi
	cmpw	%di, %r8w
	jb	.L36
	cmpl	$3, %edi
	ja	.L14
	addl	$1, %r10d
	jmp	.L15
	.p2align 4,,10
	.p2align 3
.L30:
	.cfi_def_cfa_offset 8
	.cfi_restore 3
	.cfi_restore 6
	movl	$9, 4(%rsi)
	xorl	%eax, %eax
	ret
	.p2align 4,,10
	.p2align 3
.L35:
	.cfi_def_cfa_offset 24
	.cfi_offset 3, -24
	.cfi_offset 6, -16
	movw	%r10w, 30(%rsi)
	movw	%r11w, 26(%rsi)
	testw	%r9w, %r9w
	je	.L17
	movl	$6, 4(%rsi)
	jmp	.L6
.L17:
	imull	$52429, %edx, %edx
	shrl	$20, %edx
	cmpl	%r10d, %edx
	jnb	.L18
	movl	$5, 4(%rsi)
	jmp	.L6
.L18:
	movl	$1, %eax
	cmpl	%r11d, %edx
	jnb	.L5
	movl	$2, 4(%rsi)
	jmp	.L6
	.cfi_endproc
.LFE11:
	.size	check_frame, .-check_frame
	.p2align 4
	.type	finish, @function
finish:
.LFB12:
	.cfi_startproc
	movl	%edx, %r10d
	movl	%ecx, %edx
	pushq	%r12
	.cfi_def_cfa_offset 16
	.cfi_offset 12, -16
	pushq	%rbp
	.cfi_def_cfa_offset 24
	.cfi_offset 6, -24
	pushq	%rbx
	.cfi_def_cfa_offset 32
	.cfi_offset 3, -32
	leal	-98000(%rdx), %ebx
	movl	32(%rsp), %ecx
	movl	40(%rsp), %r11d
	movl	48(%rsp), %eax
	cmpl	$304000, %ebx
	ja	.L41
	cmpl	$1310719, %r8d
	jbe	.L42
	cmpw	$759, %cx
	jbe	.L44
	movzwl	%r11w, %ebp
	movzwl	%ax, %eax
	movzwl	%cx, %ebx
	addl	%ebp, %eax
	cmpl	%eax, %ebx
	jl	.L44
	movl	12(%rdi), %eax
	movl	%edx, %r12d
	movl	%edx, 8(%rsi)
	xorl	%edx, %edx
	movl	%r8d, 56(%rsi)
	movl	%r8d, %r8d
	salq	$16, %rax
	movl	%r10d, (%rsi)
	divq	%r12
	xorl	%edx, %edx
	movl	%eax, 52(%rsi)
	movl	16(%rdi), %eax
	imulq	%r8, %rax
	movzwl	10(%rdi), %r8d
	movl	20(%rdi), %edi
	salq	$16, %r8
	divq	%r8
	xorl	%edx, %edx
	imulq	$1000000, %rax, %rax
	divq	%rdi
	movl	$4294967295, %edx
	cmpq	%rdx, %rax
	cmova	%rdx, %rax
	movabsq	$2361183241434822607, %rdx
	movl	%eax, 44(%rsi)
	addq	$500, %rax
	shrq	$3, %rax
	mulq	%rdx
	shrq	$4, %rdx
	movl	%edx, 12(%rsi)
	cmpl	$1, %r10d
	jne	.L39
	leal	500(%r9), %eax
	movl	%r9d, 48(%rsi)
	imulq	$274877907, %rax, %rax
	shrq	$38, %rax
	movw	%ax, 16(%rsi)
.L39:
	movd	%ecx, %xmm1
	subl	%ebp, %ebx
	subl	%r11d, %ecx
	movw	%r11w, 22(%rsi)
	pshuflw	$0, %xmm1, %xmm0
	movw	%cx, 62(%rsi)
	movd	%xmm0, 18(%rsi)
	cmpl	$14, %ebx
	jg	.L40
	orw	$8, 60(%rsi)
.L40:
	xorl	%edx, %edx
	popq	%rbx
	.cfi_remember_state
	.cfi_def_cfa_offset 24
	movl	$1, %eax
	popq	%rbp
	.cfi_def_cfa_offset 16
	movl	%edx, 4(%rsi)
	popq	%r12
	.cfi_def_cfa_offset 8
	ret
	.p2align 4,,10
	.p2align 3
.L42:
	.cfi_restore_state
	movl	$1, %edx
	popq	%rbx
	.cfi_remember_state
	.cfi_def_cfa_offset 24
	xorl	%eax, %eax
	popq	%rbp
	.cfi_def_cfa_offset 16
	movl	%edx, 4(%rsi)
	popq	%r12
	.cfi_def_cfa_offset 8
	ret
	.p2align 4,,10
	.p2align 3
.L44:
	.cfi_restore_state
	movl	$7, %edx
	popq	%rbx
	.cfi_remember_state
	.cfi_def_cfa_offset 24
	xorl	%eax, %eax
	popq	%rbp
	.cfi_def_cfa_offset 16
	movl	%edx, 4(%rsi)
	popq	%r12
	.cfi_def_cfa_offset 8
	ret
	.p2align 4,,10
	.p2align 3
.L41:
	.cfi_restore_state
	movl	$4, %edx
	popq	%rbx
	.cfi_def_cfa_offset 24
	xorl	%eax, %eax
	popq	%rbp
	.cfi_def_cfa_offset 16
	movl	%edx, 4(%rsi)
	popq	%r12
	.cfi_def_cfa_offset 8
	ret
	.cfi_endproc
.LFE12:
	.size	finish, .-finish
	.p2align 4
	.type	remove_harmonic, @function
remove_harmonic:
.LFB23:
	.cfi_startproc
	movq	%rsi, %r9
	pushq	%r14
	.cfi_def_cfa_offset 16
	.cfi_offset 14, -16
	movq	%rdi, %r11
	leaq	128(%rdi), %r10
	pushq	%r13
	.cfi_def_cfa_offset 24
	.cfi_offset 13, -24
	salq	$32, %r9
	xorl	%r13d, %r13d
	pushq	%r12
	.cfi_def_cfa_offset 32
	.cfi_offset 12, -32
	movq	%rdi, %r12
	pushq	%rbp
	.cfi_def_cfa_offset 40
	.cfi_offset 6, -40
	xorl	%ebp, %ebp
	pushq	%rbx
	.cfi_def_cfa_offset 48
	.cfi_offset 3, -48
	xorl	%ebx, %ebx
	.p2align 4,,10
	.p2align 3
.L47:
	movq	%r13, %r14
	movswl	(%r12), %r8d
	addq	$2, %r12
	addq	%r9, %r13
	shrq	$6, %r14
	movl	%r14d, %edi
	call	sin_q10
	leal	1073741824(%r14), %edi
	imull	%r8d, %eax
	cltq
	addq	%rax, %rbx
	call	sin_q10
	imull	%eax, %r8d
	movslq	%r8d, %r8
	addq	%r8, %rbp
	cmpq	%r12, %r10
	jne	.L47
	testq	%rbx, %rbx
	leaq	32767(%rbx), %rax
	leaq	32767(%rbp), %r8
	cmovs	%rax, %rbx
	sarq	$15, %rbx
	testq	%rbp, %rbp
	cmovns	%rbp, %r8
	xorl	%r12d, %r12d
	sarq	$15, %r8
	.p2align 4,,10
	.p2align 3
.L48:
	movq	%r12, %rbp
	shrq	$6, %rbp
	movl	%ebp, %edi
	call	sin_q10
	leal	1073741824(%rbp), %edi
	imull	%ebx, %eax
	movl	%eax, %r13d
	call	sin_q10
	imull	%r8d, %eax
	addl	%r13d, %eax
	leal	1023(%rax), %edx
	cmovs	%edx, %eax
	addq	%r9, %r12
	sarl	$10, %eax
	subw	%ax, (%r11)
	addq	$2, %r11
	cmpq	%r11, %r10
	jne	.L48
	popq	%rbx
	.cfi_def_cfa_offset 40
	popq	%rbp
	.cfi_def_cfa_offset 32
	popq	%r12
	.cfi_def_cfa_offset 24
	popq	%r13
	.cfi_def_cfa_offset 16
	popq	%r14
	.cfi_def_cfa_offset 8
	ret
	.cfi_endproc
.LFE23:
	.size	remove_harmonic, .-remove_harmonic
	.p2align 4
	.type	fit_template, @function
fit_template:
.LFB24:
	.cfi_startproc
	pushq	%r15
	.cfi_def_cfa_offset 16
	.cfi_offset 15, -16
	movl	%esi, %eax
	movl	%edx, %esi
	xorl	%r15d, %r15d
	pushq	%r14
	.cfi_def_cfa_offset 24
	.cfi_offset 14, -24
	movl	%eax, %edx
	salq	$16, %rsi
	pushq	%r13
	.cfi_def_cfa_offset 32
	.cfi_offset 13, -32
	salq	$16, %rdx
	xorl	%r13d, %r13d
	pushq	%r12
	.cfi_def_cfa_offset 40
	.cfi_offset 12, -40
	movq	%rdx, %rax
	movl	%edi, %r12d
	pushq	%rbp
	.cfi_def_cfa_offset 48
	.cfi_offset 6, -48
	movq	%r9, %rbp
	movq	%rsi, %r9
	pushq	%rbx
	.cfi_def_cfa_offset 56
	.cfi_offset 3, -56
	leaq	model(%rip), %rbx
	subq	$40, %rsp
	.cfi_def_cfa_offset 96
	movq	%rcx, 24(%rsp)
	movabsq	$4835703278458516699, %rcx
	mulq	%rcx
	movq	%rsi, %rax
	movq	%r8, 16(%rsp)
	shrq	$18, %rdx
	movq	%rdx, %r8
	movl	%edx, %r11d
	mulq	%rcx
	movq	%rdx, %r10
	shrq	$19, %r10
	movl	%r10d, %eax
	movl	%r10d, %r14d
	xorl	%r10d, %r10d
	negl	%eax
	movl	%eax, 12(%rsp)
	.p2align 4,,10
	.p2align 3
.L68:
	movswl	0(%rbp,%r10), %eax
	addl	%eax, %r13d
	movq	%r10, %rax
	salq	$31, %rax
	shrq	$6, %rax
	movl	%eax, %edi
	testl	%r12d, %r12d
	je	.L136
	shrl	$16, %eax
	movl	%eax, %esi
	cmpl	$2, %r12d
	je	.L137
	cmpl	$3, %r12d
	je	.L138
	movl	%eax, %edx
	subl	%r11d, %edx
	cmpl	$32767, %eax
	ja	.L59
	cmpl	$32767, %edx
	jle	.L60
	cmpq	$1999999, %r9
	jbe	.L98
	cmpl	%eax, %r14d
	jl	.L62
.L61:
	movl	%esi, %eax
.L135:
	sall	$10, %eax
	cltd
	idivl	%r14d
	movl	%eax, %esi
	movswl	%ax, %edx
	.p2align 4,,10
	.p2align 3
.L54:
	movw	%si, (%rbx,%r10)
	addq	$2, %r10
	addl	%edx, %r15d
	cmpq	$128, %r10
	jne	.L68
	movl	96(%rsp), %eax
	testl	%eax, %eax
	jne	.L139
.L69:
	xorl	%edi, %edi
	xorl	%esi, %esi
	xorl	%ecx, %ecx
	.p2align 4,,10
	.p2align 3
.L71:
	movswl	0(%rbp,%rdi), %eax
	sall	$6, %eax
	subl	%r13d, %eax
	cltq
	imulq	%rax, %rax
	addq	%rax, %rcx
	movswl	(%rbx,%rdi), %eax
	addq	$2, %rdi
	sall	$6, %eax
	subl	%r15d, %eax
	cltq
	imulq	%rax, %rax
	addq	%rax, %rsi
	cmpq	$128, %rdi
	jne	.L71
	testq	%rcx, %rcx
	je	.L101
	testq	%rsi, %rsi
	je	.L101
	xorl	%r8d, %r8d
	xorl	%r14d, %r14d
	xorl	%edi, %edi
	.p2align 4,,10
	.p2align 3
.L73:
	xorl	%r9d, %r9d
	xorl	%r10d, %r10d
	.p2align 4,,10
	.p2align 3
.L74:
	leal	(%r14,%r9), %eax
	movswl	0(%rbp,%r9,2), %edx
	addq	$1, %r9
	andl	$63, %eax
	movswl	(%rbx,%rax,2), %eax
	sall	$6, %edx
	subl	%r13d, %edx
	sall	$6, %eax
	movslq	%edx, %rdx
	subl	%r15d, %eax
	cltq
	imulq	%rdx, %rax
	addq	%rax, %r10
	cmpq	$64, %r9
	jne	.L74
	testq	%r10, %r10
	jle	.L75
	cmpq	%r10, %rdi
	setb	%al
	testb	%al, %al
	cmovne	%r14d, %r8d
	cmovne	%r10, %rdi
.L75:
	addl	$1, %r14d
	cmpl	$64, %r14d
	jne	.L73
	movq	24(%rsp), %rax
	movq	%rsi, %r10
	shrq	$16, %r10
	movw	%r8w, (%rax)
	je	.L140
	movq	%rdi, %r9
	movq	%rcx, %r13
	xorl	%edx, %edx
	shrq	$16, %r9
	shrq	$16, %r13
	movq	%r9, %rax
	imulq	%r10, %r13
	imulq	%r9, %rax
	imulq	$1000, %rax, %rax
	divq	%r13
	xorl	%edx, %edx
	movq	%rax, %r11
	movq	%r9, %rax
	salq	$27, %rax
	divq	%r10
	movl	$4294967295, %edx
	cmpq	%rdx, %rax
	cmova	%rdx, %rax
	movq	16(%rsp), %rdx
	movl	%eax, (%rdx)
	cmpl	$3, %r12d
	je	.L79
.L82:
	cmpq	$0, 104(%rsp)
	je	.L92
.L80:
	shrq	$20, %rdi
	shrq	$20, %rcx
	shrq	$20, %rsi
	cmpq	$1000000, %rdi
	jbe	.L93
.L94:
	shrq	%rdi
	shrq	%rcx
	shrq	%rsi
	cmpq	$1000000, %rdi
	ja	.L94
.L93:
	testq	%rcx, %rcx
	je	.L105
	testq	%rsi, %rsi
	je	.L105
	imulq	%rdi, %rdi
	xorl	%edx, %edx
	imulq	%rsi, %rcx
	imulq	$1000000, %rdi, %rax
	divq	%rcx
	movl	$1000000, %edx
	cmpq	$1000000, %rax
	cmova	%edx, %eax
.L96:
	movq	104(%rsp), %rcx
	movl	%eax, (%rcx)
.L92:
	cmpq	$1000, %r11
	movl	$1000, %eax
	cmovbe	%r11d, %eax
	addq	$40, %rsp
	.cfi_remember_state
	.cfi_def_cfa_offset 56
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
.L60:
	.cfi_restore_state
	cmpl	$-32768, %edx
	jge	.L64
	cmpq	$1999999, %r9
	jbe	.L99
	cmpl	%eax, %r14d
	jge	.L61
.L62:
	cmpq	%r8, %rax
	sbbl	%edx, %edx
	andl	$2048, %edx
	subl	$1024, %edx
	cmpq	%r8, %rax
	sbbl	%esi, %esi
	andw	$2048, %si
	subw	$1024, %si
	jmp	.L54
.L136:
	call	sin_q10
	movl	%eax, %esi
	movswl	%ax, %edx
	jmp	.L54
.L59:
	leal	-65536(%rax), %esi
	cmpl	$32767, %edx
	jle	.L63
	cmpq	$1999999, %r9
	jbe	.L98
	movl	12(%rsp), %eax
	cmpl	%eax, %esi
	jge	.L61
.L98:
	movl	$-1024, %edx
	movl	$-1024, %esi
	jmp	.L54
.L137:
	movl	%eax, %edx
	sarl	$4, %edx
	movl	%edx, %esi
	cmpl	$16383, %eax
	jbe	.L54
	cmpl	$49151, %eax
	ja	.L57
	movl	$2048, %esi
	subl	%edx, %esi
	movswl	%si, %edx
	jmp	.L54
.L138:
	sarl	$5, %eax
	movl	$1024, %esi
	subl	%eax, %esi
	movswl	%si, %edx
	jmp	.L54
.L63:
	cmpl	$-32768, %edx
	jge	.L66
	cmpq	$1999999, %r9
	jbe	.L99
	movl	12(%rsp), %eax
	cmpl	%eax, %esi
	jge	.L61
.L99:
	movl	$1024, %edx
	movl	$1024, %esi
	jmp	.L54
.L64:
	cmpq	$1999999, %r9
	jbe	.L62
	cmpl	%eax, %r14d
	jge	.L61
.L67:
	movl	12(%rsp), %ecx
	cmpl	%ecx, %edx
	jl	.L62
	cmpl	%r14d, %edx
	jg	.L62
	movl	%edx, %eax
	negl	%eax
	jmp	.L135
.L66:
	cmpq	$1999999, %r9
	jbe	.L62
	movl	12(%rsp), %ecx
	cmpl	%ecx, %esi
	jge	.L61
	jmp	.L67
.L57:
	leal	-4096(%rdx), %esi
	movswl	%si, %edx
	jmp	.L54
.L140:
	movq	16(%rsp), %rax
	xorl	%r11d, %r11d
	movl	$0, (%rax)
	cmpl	$3, %r12d
	je	.L79
	cmpq	$0, 104(%rsp)
	jne	.L80
.L101:
	addq	$40, %rsp
	.cfi_remember_state
	.cfi_def_cfa_offset 56
	xorl	%eax, %eax
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
.L139:
	.cfi_restore_state
	movl	96(%rsp), %esi
	movq	%rbx, %rdi
	pxor	%xmm0, %xmm0
	pxor	%xmm4, %xmm4
	call	remove_harmonic
	leaq	model(%rip), %rax
	leaq	128(%rax), %rdx
.L70:
	movdqa	(%rax), %xmm2
	movdqa	%xmm4, %xmm3
	addq	$16, %rax
	pcmpgtw	%xmm2, %xmm3
	movdqa	%xmm2, %xmm1
	punpcklwd	%xmm3, %xmm1
	punpckhwd	%xmm3, %xmm2
	paddd	%xmm1, %xmm0
	paddd	%xmm2, %xmm0
	cmpq	%rax, %rdx
	jne	.L70
	movdqa	%xmm0, %xmm1
	psrldq	$8, %xmm1
	paddd	%xmm1, %xmm0
	movdqa	%xmm0, %xmm1
	psrldq	$4, %xmm1
	paddd	%xmm1, %xmm0
	movd	%xmm0, %r15d
	jmp	.L69
.L79:
	xorl	%eax, %eax
	xorl	%r10d, %r10d
	xorl	%r14d, %r14d
	xorl	%r13d, %r13d
.L84:
	leal	(%r8,%rax), %edx
	andl	$63, %edx
	movswq	(%rbx,%rdx,2), %rdx
	movq	%rdx, %r9
	addl	$800, %edx
	cmpl	$1600, %edx
	ja	.L83
	movswq	0(%rbp,%rax,2), %rdx
	addq	%r9, %r14
	addl	$1, %r10d
	addq	%rdx, %r13
.L83:
	addq	$1, %rax
	cmpq	$64, %rax
	jne	.L84
	xorl	%edx, %edx
	xorl	%r12d, %r12d
	xorl	%r9d, %r9d
.L86:
	leal	(%r8,%rdx), %eax
	andl	$63, %eax
	movswq	(%rbx,%rax,2), %r15
	movq	%r15, %rax
	addl	$800, %r15d
	cmpl	$1600, %r15d
	ja	.L85
	movswq	0(%rbp,%rdx,2), %r15
	imulq	%r10, %rax
	imulq	%r10, %r15
	subq	%r14, %rax
	subq	%r13, %r15
	imulq	%rax, %r15
	imulq	%rax, %rax
	addq	%r15, %r9
	addq	%rax, %r12
.L85:
	addq	$1, %rdx
	cmpq	$64, %rdx
	jne	.L86
	testq	%r9, %r9
	jle	.L82
	testq	%r12, %r12
	jle	.L82
	movabsq	$137438953471, %rax
	cmpq	%r9, %rax
	jnb	.L89
.L90:
	shrq	%r9
	shrq	%r12
	cmpq	%r9, %rax
	jb	.L90
.L89:
	movq	%r9, %rax
	xorl	%edx, %edx
	movq	16(%rsp), %rbx
	salq	$27, %rax
	divq	%r12
	movl	$4294967295, %edx
	cmpq	%rdx, %rax
	cmova	%rdx, %rax
	movl	%eax, (%rbx)
	jmp	.L82
.L105:
	xorl	%eax, %eax
	jmp	.L96
	.cfi_endproc
.LFE24:
	.size	fit_template, .-fit_template
	.p2align 4
	.type	refine_dc, @function
refine_dc:
.LFB27:
	.cfi_startproc
	movl	52(%rsi), %eax
	testl	%eax, %eax
	je	.L155
	movzwl	8(%rdi), %r10d
	movl	%eax, %ecx
	xorl	%edx, %edx
	movq	%rdi, %r9
	leal	-1(%r10), %eax
	salq	$16, %rax
	divq	%rcx
	testl	%eax, %eax
	je	.L155
	movl	%eax, %edi
	pushq	%r12
	.cfi_def_cfa_offset 16
	.cfi_offset 12, -16
	movq	%rsi, %r8
	imulq	%rcx, %rdi
	pushq	%rbp
	.cfi_def_cfa_offset 24
	.cfi_offset 6, -24
	pushq	%rbx
	.cfi_def_cfa_offset 32
	.cfi_offset 3, -32
	movq	(%r9), %rbx
	movq	%rdi, %r11
	movzwl	(%rbx), %esi
	movzwl	%di, %r12d
	shrq	$16, %r11
	movl	%r11d, %eax
	movzwl	(%rbx,%rax,2), %ebp
	addq	%rbp, %rsi
	salq	$15, %rsi
	cmpl	$1, %r11d
	jbe	.L143
	leal	-2(%r11), %eax
	leaq	2(%rbx), %rdx
	leaq	4(%rbx,%rax,2), %rax
	.p2align 4,,10
	.p2align 3
.L144:
	movzwl	(%rdx), %ecx
	addq	$2, %rdx
	salq	$16, %rcx
	addq	%rcx, %rsi
	cmpq	%rdx, %rax
	jne	.L144
.L143:
	testl	%r12d, %r12d
	je	.L145
	leal	1(%r11), %eax
	cmpl	%r10d, %eax
	jb	.L158
.L145:
	movq	%rdi, %rax
	xorl	%edx, %edx
	shrq	%rax
	addq	%rsi, %rax
	divq	%rdi
	cmpl	$1, (%r8)
	movq	%rax, %rcx
	jne	.L146
	movl	48(%r8), %esi
	movl	56(%r8), %eax
	subq	$500000, %rsi
	imulq	%rax, %rsi
	movabsq	$4835703278458516699, %rax
	imulq	%rsi
	sarq	$63, %rsi
	sarq	$34, %rdx
	subq	%rdx, %rsi
	addq	%rsi, %rcx
.L146:
	xorl	%eax, %eax
	testq	%rcx, %rcx
	cmovs	%rax, %rcx
	movzwl	10(%r9), %eax
	cmpq	%rax, %rcx
	cmovg	%rax, %rcx
	movw	%cx, 28(%r8)
	popq	%rbx
	.cfi_def_cfa_offset 24
	popq	%rbp
	.cfi_def_cfa_offset 16
	popq	%r12
	.cfi_def_cfa_offset 8
	ret
	.p2align 4,,10
	.p2align 3
.L155:
	.cfi_restore 3
	.cfi_restore 6
	.cfi_restore 12
	ret
	.p2align 4,,10
	.p2align 3
.L158:
	.cfi_def_cfa_offset 32
	.cfi_offset 3, -32
	.cfi_offset 6, -24
	.cfi_offset 12, -16
	movzwl	(%rbx,%rax,2), %eax
	movq	%rbp, %rdx
	salq	$16, %rdx
	subq	%rbp, %rax
	imulq	%r12, %rax
	leaq	(%rax,%rdx,2), %rdx
	imulq	%r12, %rdx
	testq	%rdx, %rdx
	leaq	131071(%rdx), %rax
	cmovns	%rdx, %rax
	sarq	$17, %rax
	addq	%rax, %rsi
	jmp	.L145
	.cfi_endproc
.LFE27:
	.size	refine_dc, .-refine_dc
	.p2align 4
	.type	robust_fold, @function
robust_fold:
.LFB19:
	.cfi_startproc
	movl	12(%rdi), %ecx
	movq	%rsi, %rax
	xorl	%edx, %edx
	leaq	rb(%rip), %r8
	salq	$32, %rax
	movq	%rdi, %r10
	movq	%r8, %rdi
	divq	%rcx
	movl	$96, %ecx
	movq	%rax, %r9
	xorl	%eax, %eax
	rep stosq
	movzwl	8(%r10), %edx
	testw	%dx, %dx
	je	.L160
	movq	(%r10), %rcx
	leal	-1(%rdx), %eax
	xorl	%esi, %esi
	leaq	2(%rcx), %rdi
	leaq	(%rdi,%rax,2), %r10
	xorl	%eax, %eax
	jmp	.L165
	.p2align 4,,10
	.p2align 3
.L189:
	leaq	(%r8,%r11,4), %r11
	cmpw	4(%r11), %cx
	jnb	.L162
	movw	%cx, 4(%r11)
.L162:
	leaq	(%rdx,%rdx,2), %r11
	cmpw	%cx, 6(%r8,%r11,4)
	jnb	.L164
.L163:
	leaq	(%rdx,%rdx,2), %r11
	movw	%cx, 6(%r8,%r11,4)
.L164:
	leaq	(%rdx,%rdx,2), %rdx
	addl	$1, %esi
	addl	%r9d, %eax
	leaq	(%r8,%rdx,4), %rdx
	addl	%ecx, (%rdx)
	movq	%rdi, %rcx
	movw	%si, 8(%rdx)
	cmpq	%r10, %rdi
	je	.L160
	movl	%eax, %edx
	addq	$2, %rdi
	shrl	$26, %edx
	leaq	(%rdx,%rdx,2), %rdx
	movzwl	8(%r8,%rdx,4), %esi
.L165:
	movl	%eax, %edx
	movzwl	(%rcx), %ecx
	shrl	$26, %edx
	leaq	(%rdx,%rdx,2), %r11
	testw	%si, %si
	jne	.L189
	movw	%cx, 4(%r8,%r11,4)
	jmp	.L163
.L160:
	leaq	rb(%rip), %rsi
	leaq	folded(%rip), %r11
	movq	%r11, %rdi
	leaq	768(%rsi), %r9
	jmp	.L168
	.p2align 4,,10
	.p2align 3
.L191:
	movzwl	4(%rsi), %ecx
	movl	(%rsi), %eax
	subl	%ecx, %eax
	movzwl	6(%rsi), %ecx
	subl	%ecx, %eax
	leal	-2(%rdx), %ecx
	cltd
	idivl	%ecx
.L167:
	movw	%ax, (%rdi)
	addq	$12, %rsi
	addq	$2, %rdi
	cmpq	%r9, %rsi
	je	.L190
.L168:
	movzwl	8(%rsi), %edx
	cmpw	$2, %dx
	ja	.L191
	xorl	%eax, %eax
	testw	%dx, %dx
	je	.L167
	movl	(%rsi), %eax
	movzwl	%dx, %ecx
	addq	$12, %rsi
	addq	$2, %rdi
	cltd
	idivl	%ecx
	movw	%ax, -2(%rdi)
	cmpq	%r9, %rsi
	jne	.L168
.L190:
	leaq	8+rb(%rip), %r10
	xorl	%r9d, %r9d
	jmp	.L175
	.p2align 4,,10
	.p2align 3
.L169:
	addq	$1, %r9
	addq	$12, %r10
	cmpq	$64, %r9
	je	.L192
.L175:
	cmpw	$0, (%r10)
	movl	%r9d, %edi
	movl	$1, %ecx
	je	.L170
	addq	$1, %r9
	addq	$12, %r10
	cmpq	$64, %r9
	jne	.L175
.L192:
	ret
	.p2align 4,,10
	.p2align 3
.L172:
	addl	$1, %ecx
	cmpl	$64, %ecx
	je	.L171
.L170:
	movl	%edi, %eax
	subl	%ecx, %eax
	andl	$63, %eax
	leaq	(%rax,%rax,2), %rax
	cmpw	$0, 8(%r8,%rax,4)
	je	.L172
.L171:
	movl	$1, %esi
	jmp	.L173
	.p2align 4,,10
	.p2align 3
.L174:
	addl	$1, %esi
	cmpl	$64, %esi
	je	.L169
.L173:
	leal	(%rdi,%rsi), %edx
	andl	$63, %edx
	leaq	(%rdx,%rdx,2), %rax
	cmpw	$0, 8(%r8,%rax,4)
	je	.L174
	movl	%ecx, %eax
	orl	%esi, %eax
	cmpl	$63, %eax
	ja	.L169
	subl	%ecx, %edi
	movswl	(%r11,%rdx,2), %edx
	andl	$63, %edi
	movswl	(%r11,%rdi,2), %eax
	imull	%ecx, %edx
	addl	%esi, %ecx
	imull	%esi, %eax
	addl	%edx, %eax
	cltd
	idivl	%ecx
	movw	%ax, (%r11,%r9,2)
	jmp	.L169
	.cfi_endproc
.LFE19:
	.size	robust_fold, .-robust_fold
	.p2align 4
	.type	yin_period.constprop.0, @function
yin_period.constprop.0:
.LFB29:
	.cfi_startproc
	pushq	%r15
	.cfi_def_cfa_offset 16
	.cfi_offset 15, -16
	pushq	%r14
	.cfi_def_cfa_offset 24
	.cfi_offset 14, -24
	movq	%rsi, %r14
	movl	$2800746037, %esi
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
	subq	$664, %rsp
	.cfi_def_cfa_offset 720
	movq	%fs:40, %rax
	movq	%rax, 648(%rsp)
	xorl	%eax, %eax
	movl	12(%rdi), %eax
	imulq	%rax, %rsi
	leaq	392000(%rax), %rcx
	movq	%rax, %r8
	movabsq	$385499304724052671, %rax
	movq	%rcx, %rdx
	shrq	$6, %rdx
	mulq	%rdx
	shrq	$52, %rsi
	shrq	$7, %rdx
	movl	%edx, %edi
	cmpl	$9647999, %r8d
	jbe	.L217
	cmpq	$50568000, %rcx
	movl	$128, %eax
	cmovnb	%eax, %edi
.L194:
	cmpl	%esi, %edi
	jb	.L206
	movl	%esi, 12(%rsp)
	movzwl	8(%rbp), %r11d
	leal	1(%rdi), %r12d
	xorl	%ebx, %ebx
	leal	0(,%rdi,4), %r9d
	movl	$4, %r10d
	movl	$1, %r8d
	leaq	128(%rsp), %r13
	.p2align 4,,10
	.p2align 3
.L197:
	xorl	%eax, %eax
	cmpl	%r11d, %r9d
	jnb	.L201
	movq	0(%rbp), %rsi
	xorl	%eax, %eax
	xorl	%ecx, %ecx
	.p2align 4,,10
	.p2align 3
.L198:
	movl	%ecx, %edx
	leal	(%rcx,%r10), %r15d
	addl	$4, %ecx
	movzwl	(%rsi,%rdx,2), %edx
	movzwl	(%rsi,%r15,2), %r15d
	subl	%r15d, %edx
	imull	%edx, %edx
	movslq	%edx, %rdx
	addq	%rdx, %rax
	leal	(%rcx,%r9), %edx
	cmpl	%r11d, %edx
	jb	.L198
	addq	%rax, %rbx
	imulq	%r8, %rax
	movl	$1, %ecx
	imulq	$1000, %rax, %rax
	testq	%rbx, %rbx
	cmovne	%rbx, %rcx
	xorl	%edx, %edx
	divq	%rcx
.L201:
	movl	%eax, 0(%r13,%r8,4)
	addq	$1, %r8
	addl	$4, %r10d
	cmpq	%r8, %r12
	jne	.L197
	movl	12(%rsp), %eax
	xorl	%r10d, %r10d
	movl	$-1, %r12d
	movq	%rax, %rsi
	leaq	0(%r13,%rax,4), %rcx
	jmp	.L205
	.p2align 4,,10
	.p2align 3
.L203:
	addq	$4, %rcx
	cmpl	%eax, %edi
	je	.L241
	movl	%ebx, %eax
.L205:
	movl	(%rcx), %edx
	cmpl	%r12d, %edx
	jnb	.L202
	movl	%edx, %r12d
	movl	%eax, %r10d
.L202:
	cmpl	%esi, %eax
	leal	1(%rax), %ebx
	setnb	%r11b
	cmpl	%edi, %eax
	setb	%r8b
	testb	%r8b, %r11b
	je	.L203
	cmpl	$539, %edx
	ja	.L203
	leal	-1(%rax), %r8d
	cmpl	%edx, 128(%rsp,%r8,4)
	jb	.L203
	cmpl	4(%rcx), %edx
	jnb	.L203
	movw	%dx, (%r14)
	movl	%eax, %r10d
	jmp	.L215
	.p2align 4,,10
	.p2align 3
.L241:
	movw	%r12w, (%r14)
	cmpl	$650, %r12d
	ja	.L206
	testl	%r10d, %r10d
	je	.L206
.L215:
	sall	$2, %r10d
	movl	$7, %eax
	cmpl	%eax, %r10d
	leal	6(%r10), %ebx
	cmovnb	%r10d, %eax
	leal	-6(%rax), %edx
	cmpl	%edx, %ebx
	jb	.L207
	movzwl	8(%rbp), %r8d
	addl	$6, %r9d
	leaq	16(%rsp), %r11
	movl	%edx, %edi
	movq	$-1, %r12
	.p2align 4,,10
	.p2align 3
.L208:
	xorl	%esi, %esi
	cmpl	%r8d, %r9d
	jnb	.L212
	movq	0(%rbp), %r13
	xorl	%esi, %esi
	xorl	%ecx, %ecx
	.p2align 4,,10
	.p2align 3
.L209:
	movl	%ecx, %eax
	leal	(%rcx,%rdi), %r14d
	addl	$2, %ecx
	movzwl	0(%r13,%rax,2), %eax
	movzwl	0(%r13,%r14,2), %r14d
	subl	%r14d, %eax
	imull	%eax, %eax
	cltq
	addq	%rax, %rsi
	leal	(%rcx,%r9), %eax
	cmpl	%r8d, %eax
	jb	.L209
.L212:
	movq	%rsi, (%r11)
	cmpq	%r12, %rsi
	jnb	.L210
	movq	%rsi, %r12
	movl	%edi, %r10d
.L210:
	addl	$1, %edi
	addq	$8, %r11
	cmpl	%edi, %ebx
	jnb	.L208
.L207:
	cmpl	%r10d, %edx
	jnb	.L222
	cmpl	%ebx, %r10d
	jnb	.L222
	movl	%r10d, %eax
	subl	%edx, %eax
	leal	1(%rax), %ecx
	leal	-1(%rax), %edx
	movq	16(%rsp,%rax,8), %rax
	movq	16(%rsp,%rdx,8), %rdx
	movq	16(%rsp,%rcx,8), %rsi
	addq	%rax, %rax
	leaq	(%rdx,%rsi), %rcx
	subq	%rax, %rcx
	xorl	%eax, %eax
	addq	%rcx, %rcx
	testq	%rcx, %rcx
	jle	.L213
	movq	%rdx, %rax
	subq	%rsi, %rax
	salq	$16, %rax
	cqto
	idivq	%rcx
	movl	$-32768, %edx
	cmpl	%edx, %eax
	cmovge	%eax, %edx
	cmpl	$32769, %eax
	movl	$32768, %eax
	cmovl	%edx, %eax
.L213:
	sall	$16, %r10d
	addl	%r10d, %eax
	jmp	.L193
	.p2align 4,,10
	.p2align 3
.L206:
	xorl	%eax, %eax
.L193:
	movq	648(%rsp), %rdx
	subq	%fs:40, %rdx
	jne	.L242
	addq	$664, %rsp
	.cfi_remember_state
	.cfi_def_cfa_offset 56
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
	.p2align 4,,10
	.p2align 3
.L217:
	.cfi_restore_state
	movl	$6, %esi
	jmp	.L194
.L222:
	xorl	%eax, %eax
	sall	$16, %r10d
	addl	%r10d, %eax
	jmp	.L193
.L242:
	call	__stack_chk_fail@PLT
	.cfi_endproc
.LFE29:
	.size	yin_period.constprop.0, .-yin_period.constprop.0
	.p2align 4
	.type	square_levels.isra.0, @function
square_levels.isra.0:
.LFB30:
	.cfi_startproc
	pushq	%r14
	.cfi_def_cfa_offset 16
	.cfi_offset 14, -16
	movq	%rdi, %r9
	movl	$64, %ecx
	pushq	%r13
	.cfi_def_cfa_offset 24
	.cfi_offset 13, -24
	pushq	%r12
	.cfi_def_cfa_offset 32
	.cfi_offset 12, -32
	pushq	%rbp
	.cfi_def_cfa_offset 40
	.cfi_offset 6, -40
	pushq	%rbx
	.cfi_def_cfa_offset 48
	.cfi_offset 3, -48
	movq	%rsi, %rbx
	subq	$528, %rsp
	.cfi_def_cfa_offset 576
	movzwl	8(%r9), %ebp
	movq	%fs:40, %rax
	movq	%rax, 520(%rsp)
	xorl	%eax, %eax
	movq	%rsp, %rsi
	movq	%rsi, %rdi
	rep stosq
	testl	%ebp, %ebp
	je	.L243
	movq	(%r9), %r10
	movzwl	10(%r9), %r8d
	leal	-1(%rbp), %eax
	movq	%rdx, %r11
	movq	%rbp, %r12
	leaq	2(%r10), %rdi
	addl	$1, %r8d
	movq	%r10, %rcx
	leaq	(%rdi,%rax,2), %r9
	.p2align 4,,10
	.p2align 3
.L246:
	movzwl	(%rcx), %eax
	xorl	%edx, %edx
	addq	$2, %rcx
	sall	$8, %eax
	divl	%r8d
	movl	%eax, %eax
	addw	$1, (%rsp,%rax,2)
	cmpq	%rcx, %r9
	jne	.L246
	imull	$52429, %ebp, %r14d
	movzwl	(%rsp), %ecx
	shrl	$20, %r14d
	cmpl	%r14d, %ecx
	jnb	.L259
	movl	%ecx, %edx
	movl	$1, %eax
	jmp	.L248
	.p2align 4,,10
	.p2align 3
.L260:
	movq	%r13, %rax
.L248:
	cmpq	$256, %rax
	je	.L259
	movzwl	(%rsi,%rax,2), %r13d
	addl	%r13d, %edx
	leaq	1(%rax), %r13
	cmpl	%r14d, %edx
	jb	.L260
	movl	%eax, %edx
.L247:
	leal	0(%rbp,%rbp,8), %eax
	leal	0(%rbp,%rax,2), %r13d
	movl	$3435973837, %eax
	imulq	%rax, %r13
	shrq	$36, %r13
	cmpl	%r13d, %ecx
	jnb	.L243
	movl	$1, %eax
	jmp	.L251
	.p2align 4,,10
	.p2align 3
.L267:
	movq	%rbp, %rax
.L251:
	cmpq	$256, %rax
	je	.L266
	movzwl	(%rsi,%rax,2), %ebp
	addl	%ebp, %ecx
	leaq	1(%rax), %rbp
	cmpl	%r13d, %ecx
	jb	.L267
.L253:
	addl	$1, %edx
	cmpl	%eax, %edx
	jnb	.L243
	addl	%eax, %edx
	xorl	%ebp, %ebp
	xorl	%r13d, %r13d
	xorl	%eax, %eax
	imull	%r8d, %edx
	xorl	%r8d, %r8d
	shrl	$9, %edx
	jmp	.L257
	.p2align 4,,10
	.p2align 3
.L268:
	addl	$1, %eax
	addq	%rsi, %r8
	movq	%rdi, %r10
	cmpq	%rdi, %r9
	je	.L264
.L269:
	addq	$2, %rdi
.L257:
	movzwl	(%r10), %esi
	cmpl	%edx, %esi
	jnb	.L268
	addl	$1, %r13d
	addq	%rsi, %rbp
	movq	%rdi, %r10
	cmpq	%rdi, %r9
	jne	.L269
.L264:
	testl	%eax, %eax
	je	.L243
	testl	%r13d, %r13d
	je	.L243
	movl	%eax, %ecx
	movl	%r12d, %edx
	salq	$16, %r8
	imulq	$1000000, %rcx, %rax
	shrw	%dx
	movzwl	%dx, %edx
	addq	%rdx, %rax
	xorl	%edx, %edx
	divq	%r12
	xorl	%edx, %edx
	movl	%eax, (%rbx)
	movq	%r8, %rax
	divq	%rcx
	xorl	%edx, %edx
	movq	%rax, %r8
	movq	%rbp, %rax
	salq	$16, %rax
	divq	%r13
	subl	%eax, %r8d
	movl	%r8d, (%r11)
.L243:
	movq	520(%rsp), %rax
	subq	%fs:40, %rax
	jne	.L270
	addq	$528, %rsp
	.cfi_remember_state
	.cfi_def_cfa_offset 48
	popq	%rbx
	.cfi_def_cfa_offset 40
	popq	%rbp
	.cfi_def_cfa_offset 32
	popq	%r12
	.cfi_def_cfa_offset 24
	popq	%r13
	.cfi_def_cfa_offset 16
	popq	%r14
	.cfi_def_cfa_offset 8
	ret
	.p2align 4,,10
	.p2align 3
.L259:
	.cfi_restore_state
	xorl	%edx, %edx
	jmp	.L247
.L266:
	movl	$255, %eax
	jmp	.L253
.L270:
	call	__stack_chk_fail@PLT
	.cfi_endproc
.LFE30:
	.size	square_levels.isra.0, .-square_levels.isra.0
	.p2align 4
	.type	projection.isra.0, @function
projection.isra.0:
.LFB31:
	.cfi_startproc
	movzwl	%r8w, %eax
	pushq	%r15
	.cfi_def_cfa_offset 16
	.cfi_offset 15, -16
	movzwl	%r8w, %r8d
	imulq	%rcx, %rax
	movl	%edx, %ecx
	pushq	%r14
	.cfi_def_cfa_offset 24
	.cfi_offset 14, -24
	movzwl	%si, %r14d
	shrl	%ecx
	pushq	%r13
	.cfi_def_cfa_offset 32
	.cfi_offset 13, -32
	pushq	%r12
	.cfi_def_cfa_offset 40
	.cfi_offset 12, -40
	salq	$32, %rax
	pushq	%rbp
	.cfi_def_cfa_offset 48
	.cfi_offset 6, -48
	addq	%rcx, %rax
	movl	%edx, %ecx
	xorl	%edx, %edx
	pushq	%rbx
	.cfi_def_cfa_offset 56
	.cfi_offset 3, -56
	divq	%rcx
	testw	%si, %si
	je	.L299
	movq	%rdi, %r13
	movq	%rax, %r12
	xorl	%ebp, %ebp
	xorl	%ebx, %ebx
	xorl	%r11d, %r11d
	xorl	%r15d, %r15d
	.p2align 4,,10
	.p2align 3
.L272:
	movl	%r15d, %eax
	movl	%r11d, %edi
	addl	%r8d, %r15d
	movzwl	0(%r13,%rax,2), %r10d
	call	sin_q10
	leal	1073741824(%r11), %edi
	addl	%r12d, %r11d
	subl	%r9d, %r10d
	cltq
	movslq	%r10d, %r10
	imulq	%r10, %rax
	addq	%rax, %rbx
	call	sin_q10
	cltq
	imulq	%r10, %rax
	addq	%rax, %rbp
	cmpl	%r14d, %r15d
	jb	.L272
	sarq	$4, %rbp
	sarq	$4, %rbx
	movq	%rbp, %rdi
	movq	%rbx, %rax
	imulq	%rbp, %rdi
	imulq	%rbx, %rax
	addq	%rax, %rdi
	cmpq	$0, 64(%rsp)
	je	.L278
	movq	64(%rsp), %rax
	movq	%rbp, (%rax)
	movq	%rbx, 8(%rax)
.L278:
	cmpq	$0, 56(%rsp)
	je	.L271
	movq	%rdi, %rax
	shrq	$62, %rax
	jne	.L285
.L277:
	movabsq	$4611686018427387904, %rcx
	.p2align 4,,10
	.p2align 3
.L280:
	shrq	$2, %rcx
	cmpq	%rcx, %rdi
	jb	.L280
	testq	%rcx, %rcx
	je	.L281
.L279:
	movq	%rdi, %rsi
	xorl	%eax, %eax
	.p2align 4,,10
	.p2align 3
.L283:
	leaq	(%rax,%rcx), %rdx
	shrq	%rax
	cmpq	%rdx, %rsi
	jb	.L282
	subq	%rdx, %rsi
	addq	%rcx, %rax
.L282:
	shrq	$2, %rcx
	jne	.L283
	movl	%eax, %ecx
	salq	$5, %rcx
.L281:
	leal	-1(%r14,%r8), %eax
	xorl	%edx, %edx
	movq	56(%rsp), %rbx
	divl	%r8d
	xorl	%edx, %edx
	movq	%rax, %rsi
	salq	$9, %rax
	salq	$10, %rsi
	addq	%rcx, %rax
	divq	%rsi
	movl	%eax, (%rbx)
.L271:
	popq	%rbx
	.cfi_remember_state
	.cfi_def_cfa_offset 48
	movq	%rdi, %rax
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
.L299:
	.cfi_restore_state
	cmpq	$0, 64(%rsp)
	je	.L273
	movq	64(%rsp), %rax
	cmpq	$0, 56(%rsp)
	movq	$0, (%rax)
	movq	$0, 8(%rax)
	je	.L275
.L274:
	xorl	%edi, %edi
	jmp	.L277
.L285:
	movabsq	$4611686018427387904, %rcx
	jmp	.L279
.L273:
	cmpq	$0, 56(%rsp)
	jne	.L274
.L275:
	xorl	%edi, %edi
	jmp	.L271
	.cfi_endproc
.LFE31:
	.size	projection.isra.0, .-projection.isra.0
	.section	.text.unlikely,"ax",@progbits
.LCOLDB4:
	.text
.LHOTB4:
	.p2align 4
	.globl	waveform_analyze_frequency
	.type	waveform_analyze_frequency, @function
waveform_analyze_frequency:
.LFB17:
	.cfi_startproc
	endbr64
	pushq	%r15
	.cfi_def_cfa_offset 16
	.cfi_offset 15, -16
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
	pushq	%rbx
	.cfi_def_cfa_offset 56
	.cfi_offset 3, -56
	subq	$296, %rsp
	.cfi_def_cfa_offset 352
	movq	%rdi, 64(%rsp)
	movq	%fs:40, %rax
	movq	%rax, 280(%rsp)
	xorl	%eax, %eax
	testq	%rsi, %rsi
	je	.L302
	movq	.LC0(%rip), %rax
	pxor	%xmm0, %xmm0
	movq	$0, 56(%rsi)
	movq	%rsi, %r10
	movups	%xmm0, 24(%rsi)
	movq	%rax, (%rsi)
	movl	$131330, 36(%rsi)
	movups	%xmm0, 8(%rsi)
	movups	%xmm0, 40(%rsi)
	testq	%rdi, %rdi
	je	.L303
	movl	24(%rdi), %eax
	movl	%eax, 40(%rsi)
.L303:
	movq	64(%rsp), %rbx
	movq	%r10, %rsi
	movq	%r10, (%rsp)
	movq	%rbx, %rdi
	call	check_frame
	movb	%al, 97(%rsp)
	testb	%al, %al
	je	.L302
	movzwl	8(%rbx), %edi
	movw	%di, 98(%rsp)
	testl	%edi, %edi
	je	.L305
	movq	(%rbx), %rdx
	leal	-1(%rdi), %eax
	movq	(%rsp), %r10
	movq	%rdx, 16(%rsp)
	leaq	2(%rdx,%rax,2), %rsi
	xorl	%eax, %eax
.L306:
	movzwl	(%rdx), %ecx
	addq	$2, %rdx
	addl	%ecx, %eax
	cmpq	%rsi, %rdx
	jne	.L306
	xorl	%edx, %edx
	movl	$0, 28(%rsp)
	xorl	%r12d, %r12d
	movabsq	$3367254360064000, %rcx
	divl	%edi
	movq	%r10, 80(%rsp)
	movw	%ax, 28(%r10)
	movl	%eax, %r9d
	movq	64(%rsp), %rax
	movl	12(%rax), %esi
	movzwl	8(%rax), %edx
	movl	%esi, 48(%rsp)
	movl	%esi, %eax
	movq	%rsi, 8(%rsp)
	movzwl	%dx, %esi
	shrl	%eax
	leal	-1(%rsi), %r8d
	leaq	(%rax,%rcx), %r14
	movq	%rax, 40(%rsp)
	movl	$98000, %ecx
	movl	%r8d, %eax
	movl	%esi, 24(%rsp)
	movq	16(%rsp), %rsi
	shrl	$3, %eax
	movw	%dx, 56(%rsp)
	movl	%r8d, 72(%rsp)
	salq	$4, %rax
	leaq	16(%rsi,%rax), %rbp
	.p2align 4,,10
	.p2align 3
.L309:
	movl	24(%rsp), %ebx
	movq	%r14, %rax
	xorl	%edx, %edx
	divq	8(%rsp)
	testl	%ebx, %ebx
	je	.L307
	movq	16(%rsp), %r15
	movl	%ecx, (%rsp)
	xorl	%r10d, %r10d
	xorl	%ebx, %ebx
	xorl	%r13d, %r13d
	movl	%r10d, %r11d
	movq	%r15, %r8
	movl	%eax, %r15d
	.p2align 4,,10
	.p2align 3
.L308:
	movzwl	(%r8), %r10d
	movl	%r11d, %edi
	addq	$16, %r8
	call	sin_q10
	leal	1073741824(%r11), %edi
	addl	%r15d, %r11d
	subl	%r9d, %r10d
	cltq
	movslq	%r10d, %r10
	imulq	%r10, %rax
	addq	%rax, %r13
	call	sin_q10
	cltq
	imulq	%r10, %rax
	addq	%rax, %rbx
	cmpq	%rbp, %r8
	jne	.L308
	sarq	$4, %rbx
	sarq	$4, %r13
	movl	(%rsp), %ecx
	imulq	%rbx, %rbx
	imulq	%r13, %r13
	addq	%r13, %rbx
	cmpq	%rbx, %r12
	jnb	.L307
	movl	%ecx, 28(%rsp)
	movq	%rbx, %r12
.L307:
	movabsq	$68719476736000, %rax
	addl	$2000, %ecx
	addq	%rax, %r14
	cmpl	$404000, %ecx
	jne	.L309
	movl	28(%rsp), %eax
	movl	72(%rsp), %r8d
	movq	80(%rsp), %r10
	leal	-2000(%rax), %r15d
	leal	2000(%rax), %r12d
	cmpl	%r15d, %r12d
	jb	.L310
	movq	16(%rsp), %rsi
	movl	%r8d, %eax
	movq	%r10, 72(%rsp)
	xorl	%r13d, %r13d
	shrl	$2, %eax
	movl	%r15d, %ecx
	leaq	8(%rsi,%rax,8), %rbp
.L313:
	movq	40(%rsp), %rsi
	movq	%rcx, %rax
	xorl	%edx, %edx
	movl	24(%rsp), %r10d
	salq	$34, %rax
	addq	%rsi, %rax
	divq	8(%rsp)
	movq	%rax, %r15
	testl	%r10d, %r10d
	je	.L311
	movq	16(%rsp), %r14
	movl	%ecx, (%rsp)
	xorl	%r10d, %r10d
	xorl	%r11d, %r11d
	xorl	%ebx, %ebx
	movq	%r14, %r8
	movl	%r10d, %r14d
	.p2align 4,,10
	.p2align 3
.L312:
	movzwl	(%r8), %r10d
	movl	%r14d, %edi
	addq	$8, %r8
	call	sin_q10
	leal	1073741824(%r14), %edi
	addl	%r15d, %r14d
	subl	%r9d, %r10d
	cltq
	movslq	%r10d, %r10
	imulq	%r10, %rax
	addq	%rax, %rbx
	call	sin_q10
	cltq
	imulq	%r10, %rax
	addq	%rax, %r11
	cmpq	%rbp, %r8
	jne	.L312
	sarq	$4, %r11
	sarq	$4, %rbx
	movl	(%rsp), %ecx
	imulq	%r11, %r11
	imulq	%rbx, %rbx
	addq	%rbx, %r11
	cmpq	%r11, %r13
	jnb	.L311
	movl	%ecx, 28(%rsp)
	movq	%r11, %r13
	leal	2000(%rcx), %r12d
.L311:
	addl	$100, %ecx
	cmpl	%ecx, %r12d
	jnb	.L313
	movq	72(%rsp), %r10
.L310:
	movl	28(%rsp), %eax
	xorl	%ebx, %ebx
	movq	%r10, %rbp
	leaq	208(%rsp), %r13
	movq	%rbx, %r15
	leaq	128(%rsp), %r12
	movl	%eax, %r14d
	movl	%eax, %ebx
	testq	%r15, %r15
	jne	.L410
.L314:
	pushq	%r12
	.cfi_def_cfa_offset 360
	movl	%r14d, %ecx
	addl	%ebx, %r14d
	movl	$4, %r8d
	pushq	%r13
	.cfi_def_cfa_offset 368
	movl	64(%rsp), %edx
	movl	$1, %r15d
	movzwl	72(%rsp), %esi
	movq	32(%rsp), %rdi
	call	projection.isra.0
	popq	%rax
	.cfi_def_cfa_offset 360
	popq	%rdx
	.cfi_def_cfa_offset 352
.L410:
	leaq	0(%r13,%r15,4), %rax
	pushq	$0
	.cfi_def_cfa_offset 360
	movl	$4, %r8d
	movl	%r14d, %ecx
	pushq	%rax
	.cfi_def_cfa_offset 368
	movl	64(%rsp), %edx
	addq	$1, %r15
	movzwl	72(%rsp), %esi
	movq	32(%rsp), %rdi
	call	projection.isra.0
	popq	%rdi
	.cfi_def_cfa_offset 360
	popq	%r8
	.cfi_def_cfa_offset 352
	cmpq	$5, %r15
	je	.L315
	addl	%ebx, %r14d
	testq	%r15, %r15
	jne	.L410
	jmp	.L314
.L315:
	movl	208(%rsp), %eax
	movq	%rbp, %r10
	movl	%eax, 40(%rsp)
	cmpl	$11, %eax
	ja	.L317
	movl	$1, 4(%rbp)
.L302:
	xorl	%eax, %eax
.L300:
	movq	280(%rsp), %rdx
	subq	%fs:40, %rdx
	jne	.L413
	addq	$296, %rsp
	.cfi_remember_state
	.cfi_def_cfa_offset 56
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
.L317:
	.cfi_restore_state
	movq	128(%rsp), %r8
	movq	136(%rsp), %rdi
	movq	%r8, %rdx
	movq	%rdi, %rax
	imulq	%rdi, %rax
	imulq	%r8, %rdx
	addq	%rax, %rdx
	movabsq	$4611686018427387903, %rax
	movq	%rdx, %rsi
	cmpq	%rax, %rdx
	jg	.L359
	addq	$1, %rax
.L320:
	shrq	$2, %rax
	cmpq	%rax, %rdx
	jb	.L320
	testq	%rax, %rax
	je	.L321
.L319:
	xorl	%edx, %edx
.L323:
	leaq	(%rdx,%rax), %rcx
	shrq	%rdx
	cmpq	%rcx, %rsi
	jb	.L322
	subq	%rcx, %rsi
	addq	%rax, %rdx
.L322:
	shrq	$2, %rax
	jne	.L323
	movl	28(%rsp), %eax
	movl	48(%rsp), %esi
	leal	(%rax,%rax,8), %eax
	sall	$2, %eax
	cmpl	%esi, %eax
	setb	%al
	movzbl	%al, %eax
	addl	$1, %eax
	movw	%ax, 24(%rsp)
	testl	%edx, %edx
	je	.L321
	movl	%edx, %ecx
	movl	$16384, %esi
	movq	%r10, 80(%rsp)
	movl	$1, %r12d
	movq	%rcx, %rax
	movq	%r13, 88(%rsp)
	movl	%esi, %r10d
	leaq	196(%rsp), %r15
	salq	$14, %rax
	movl	$0, (%rsp)
	movq	%rax, 72(%rsp)
	movq	%rdi, %rax
	salq	$14, %rax
	cqto
	idivq	%rcx
	movslq	%eax, %r11
	movq	%r8, %rax
	salq	$14, %rax
	movq	%r11, %r14
	cqto
	idivq	%rcx
	movl	28(%rsp), %ecx
	xorl	%edx, %edx
	movl	%edx, %r11d
	cltq
	movq	%rax, %r13
	jmp	.L328
.L327:
	movl	28(%rsp), %eax
	addl	$1, %r12d
	addl	%eax, %ecx
	cmpl	$10, %r12d
	je	.L414
.L328:
	movslq	%r10d, %rax
	movslq	%r11d, %rsi
	movq	%rax, %rdx
	movq	%rsi, %rdi
	imulq	%r14, %rdx
	imulq	%r13, %rdi
	subq	%rdi, %rdx
	leaq	16383(%rdx), %rbp
	cmovns	%rdx, %rbp
	imulq	%r13, %rax
	imulq	%r14, %rsi
	sarq	$14, %rbp
	movl	%ebp, %r10d
	addq	%rsi, %rax
	leaq	16383(%rax), %rbx
	cmovns	%rax, %rbx
	movl	$672, %eax
	sarq	$14, %rbx
	btq	%r12, %rax
	movl	%ebx, %r11d
	jnc	.L327
	leaq	144(%rsp), %rax
	movl	%ebx, 108(%rsp)
	movslq	%ebx, %rbx
	movl	%ebp, 104(%rsp)
	movslq	%ebp, %rbp
	pushq	%rax
	.cfi_def_cfa_offset 360
	pushq	$0
	.cfi_def_cfa_offset 368
	movl	40(%rsp), %r8d
	movl	64(%rsp), %edx
	movzwl	72(%rsp), %esi
	movq	32(%rsp), %rdi
	andl	$3, %r8d
	movl	%ecx, 116(%rsp)
	call	projection.isra.0
	movl	16(%rsp), %eax
	movq	88(%rsp), %r11
	movq	160(%rsp), %rdi
	movq	168(%rsp), %r8
	leaq	(%r15,%rax,4), %rsi
	movq	%rax, %rcx
	movq	%r8, %rdx
	movq	%rdi, %rax
	imulq	%rbx, %rax
	imulq	%rbp, %rdx
	addq	%rdx, %rax
	imulq	$1000, %rax, %rax
	cqto
	idivq	%r11
	movq	$-32768, %rdx
	cmpq	%rdx, %rax
	cmovl	%rdx, %rax
	movl	$32767, %edx
	cmpq	%rdx, %rax
	cmovg	%rdx, %rax
	imulq	%r8, %rbx
	movw	%ax, (%rsi)
	movq	%rdi, %rax
	imulq	%rbp, %rax
	subq	%rbx, %rax
	imulq	$1000, %rax, %rax
	cqto
	idivq	%r11
	movq	$-32768, %rdx
	cmpq	%rdx, %rax
	cmovl	%rdx, %rax
	movl	$32767, %edx
	cmpq	%rdx, %rax
	cmovg	%rdx, %rax
	addl	$1, %ecx
	addl	$1, %r12d
	movl	%ecx, 16(%rsp)
	movw	%ax, 2(%rsi)
	popq	%rcx
	.cfi_def_cfa_offset 360
	popq	%rsi
	.cfi_def_cfa_offset 352
	movl	100(%rsp), %ecx
	movl	28(%rsp), %eax
	movl	108(%rsp), %r11d
	movl	104(%rsp), %r10d
	addl	%eax, %ecx
	cmpl	$10, %r12d
	jne	.L328
.L414:
	movq	80(%rsp), %r10
	movq	88(%rsp), %r13
.L326:
	movl	40(%rsp), %r11d
	xorl	%ecx, %ecx
	movq	$-1, 264(%rsp)
	leaq	174(%rsp), %rsi
.L329:
	movl	0(%r13,%rcx,4), %eax
	xorl	%edx, %edx
	imulq	$1000, %rax, %rax
	divq	%r11
	movl	$65535, %edx
	cmpq	%rdx, %rax
	cmova	%rdx, %rax
	movw	%ax, (%rsi,%rcx,2)
	addq	$1, %rcx
	cmpq	$5, %rcx
	jne	.L329
	pxor	%xmm0, %xmm0
	movq	%rsi, %rdi
	movq	$0, 256(%rsp)
	movl	$1, %ecx
	movaps	%xmm0, 240(%rsp)
	leaq	184(%rsp), %rbp
	movl	$1000, %ebx
	leaq	240(%rsp), %r14
.L332:
	movl	$0, 184(%rsp)
	movl	%ecx, %edx
	xorl	%eax, %eax
	addl	$1, %ecx
	andl	$1, %edx
	testl	%edx, %edx
	jne	.L330
	movl	%ecx, %r8d
	movl	%ebx, %eax
	xorl	%edx, %edx
	imull	%ecx, %r8d
	divl	%r8d
.L330:
	movl	%eax, 188(%rsp)
	movzwl	2(%rdi), %r9d
	movl	%ebx, %eax
	xorl	%edx, %edx
	movq	%rbp, %r8
	divl	%ecx
	movq	%r14, %rdx
	movl	%eax, 192(%rsp)
.L331:
	movl	%r9d, %eax
	subl	(%r8), %eax
	addq	$4, %r8
	cltq
	imulq	%rax, %rax
	addq	%rax, (%rdx)
	addq	$8, %rdx
	cmpq	%r8, %r15
	jne	.L331
	addq	$2, %rdi
	cmpl	$5, %ecx
	jne	.L332
	movzwl	178(%rsp), %edx
	xorl	%edi, %edi
	movq	.LC1(%rip), %xmm0
	leal	-111(%rdx), %r12d
.L336:
	testq	%rdi, %rdi
	movl	%edx, %eax
	movl	%edx, 24(%rsp)
	movq	%r15, %r8
	movl	$12, 192(%rsp)
	cmovne	%r12d, %eax
	movq	%xmm0, 184(%rsp)
	cltq
	imulq	%rax, %rax
	movq	%rax, %rbx
	movq	%rax, (%rsp)
	movl	$1500, %eax
	cmpq	%rax, %rbx
	cmovbe	%rbx, %rax
	movq	%rbp, %rbx
	xorl	%r9d, %r9d
	movq	%rax, %rcx
.L335:
	movswl	(%r8), %eax
	testq	%rdi, %rdi
	je	.L334
	subl	(%rbx), %eax
.L334:
	movswq	2(%r8), %rdx
	cltq
	addq	$4, %r8
	addq	$4, %rbx
	imulq	%rax, %rax
	imulq	%rdx, %rdx
	addq	%rax, %rdx
	addq	%rdx, %r9
	cmpq	%r13, %r8
	jne	.L335
	movq	%rcx, %rax
	movq	(%rsp), %rcx
	movl	24(%rsp), %edx
	subq	%rcx, %rax
	leaq	(%r9,%r9,2), %rcx
	addq	%rcx, %rax
	addq	%rax, (%r14,%rdi,8)
	testq	%rdi, %rdi
	jne	.L415
	movl	$1, %edi
	jmp	.L336
.L415:
	movb	$0, 24(%rsp)
	movl	$200000, %r13d
	movq	$-1, %r15
	movabsq	$4835703278458516699, %rbp
	movl	$500000, 124(%rsp)
	leaq	8(%rsi), %r12
	movq	%r11, 16(%rsp)
	movq	%r10, 48(%rsp)
	movq	%rsi, (%rsp)
	movq	%r14, 56(%rsp)
	jmp	.L341
	.p2align 4,,10
	.p2align 3
.L337:
	addq	$10000, %r13
	cmpq	$810000, %r13
	je	.L416
.L341:
	movq	%r13, %r11
	salq	$32, %r11
	movq	%r11, %rax
	mulq	%rbp
	shrq	$19, %rdx
	movl	%edx, %edi
	call	sin_q10
	movl	%eax, %ebx
	testl	%eax, %eax
	je	.L337
	movq	%r13, %r10
	movq	(%rsp), %r8
	leal	(%rax,%rax), %r9d
	xorl	%r14d, %r14d
	salq	$33, %r10
.L340:
	movq	%r10, %rax
	mulq	%rbp
	shrq	$19, %rdx
	movl	%edx, %edi
	call	sin_q10
	movzwl	2(%r8), %ecx
	testl	%eax, %eax
	js	.L417
	cltq
	imulq	$1000, %rax, %rax
.L339:
	movl	%r9d, %esi
	xorl	%edx, %edx
	addq	$2, %r8
	addq	%r11, %r10
	divq	%rsi
	addl	%ebx, %r9d
	subl	%eax, %ecx
	movslq	%ecx, %rcx
	imulq	%rcx, %rcx
	addq	%rcx, %r14
	cmpq	%r12, %r8
	jne	.L340
	cmpq	%r15, %r14
	jnb	.L337
	movzbl	97(%rsp), %eax
	movl	%r13d, 32(%rsp)
	movq	%r14, %r15
	addq	$10000, %r13
	movb	%al, 24(%rsp)
	cmpq	$810000, %r13
	jne	.L341
.L416:
	cmpb	$0, 24(%rsp)
	movq	16(%rsp), %r11
	movq	48(%rsp), %r10
	movq	56(%rsp), %r14
	je	.L364
	movl	32(%rsp), %eax
	movq	%r15, 264(%rsp)
	movl	%eax, 124(%rsp)
.L342:
	movq	240(%rsp), %rax
	movq	248(%rsp), %rdx
	movl	$1, %ecx
	cmpq	%rax, %rdx
	jb	.L343
	movq	%rax, %rdx
	xorl	%ecx, %ecx
.L343:
	movq	256(%rsp), %rax
	cmpq	%rdx, %rax
	jb	.L366
	movq	%rdx, %rax
.L344:
	movq	264(%rsp), %rdx
	cmpq	%rax, %rdx
	jb	.L367
	movq	%rax, %rdx
.L345:
	xorl	%eax, %eax
	movl	$1, %esi
	jmp	.L347
.L419:
	movl	%esi, %edi
	movq	240(%rsp,%rdi,8), %rbx
	cmpq	%rbx, (%r14,%rax,8)
	cmovb	%eax, %esi
.L346:
	addq	$1, %rax
	cmpq	$4, %rax
	je	.L418
.L347:
	cmpl	%eax, %ecx
	je	.L346
	cmpl	%ecx, %esi
	jne	.L419
	movl	%eax, %esi
	addq	$1, %rax
	cmpq	$4, %rax
	jne	.L347
.L418:
	shrq	$3, %rdx
	movl	$1000, %r12d
	movl	%esi, %esi
	movabsq	$2361183241434822607, %r8
	movq	%rdx, %rax
	movl	%r12d, %edi
	mulq	%r8
	shrq	$4, %rdx
	subl	%edx, %edi
	movq	240(%rsp,%rsi,8), %rdx
	shrq	$3, %rdx
	movq	%rdx, %rax
	mulq	%r8
	xorl	%eax, %eax
	shrq	$4, %rdx
	subl	%edx, %r12d
	cmpl	$1001, %edi
	cmovnb	%eax, %edi
	movl	%r12d, %e