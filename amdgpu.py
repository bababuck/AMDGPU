""" Python based emulator of a Amdgpu chip

This emulator is not fully functioned, and takes several shortcuts
that limit it to simulating simple programs. It takes input from an assembly
file and parses the instructions from their string representations.

Limitations:
- Buffer loads/stores are limited, only the offset is considered when
  accessing the buffer
    - Assumes that all loads go to the a single memory region, and all stores
      go to a seperate memory region
    - This way, I can ignore the who addressing schema since:
        - I struggled to find documentation for it
        - The program I was trying to debug followed this simple model
- Only some instructions are implemented
    - Only some variants of those instructions are implemented
- Only one workgroup is simulated at a time, specified at the command line
- Assumed that the workitem.id's are loaded into v0 at program start
    - I think this is a correct assumption, I just could not find anything in
      the spec to corroborate this.
- For instructions that operate of 64 bits, I just write all 64 bits into the
  lower half register. This works if the data is later used as 64 bits (i.e.
  isn't casted to 32 bits)
"""

from abc import ABC, abstractmethod
import argparse
import re


logfile = None

class Program:
    """Represent a program loaded into chip memory.

    - Auto-incremented on accesses.
    - Is able to jump to labels

    TODO: Cache locations of labels rather than iterating through the entire
          program on each jump.
    TODO: Prune commented out lines during initial parseing
    """
    def __init__(self, filename):
        """Save the program into a list.

        Enter the program at the top of the file.

        TODO: Add a start label option?
        """
        with open(filename, 'r') as file:
            self.lines = [line.rstrip('\n').strip() for line in file]
        self.linenum = 0

    def get_next_instruction(self):
        """Fetch the next instruction from the program."""
        currline = self.lines[self.linenum]
        # Ignore if comment or label
        while currline[0] in [";", "."] or currline.split()[0][-1] == ":":
            self.linenum += 1
            currline = self.lines[self.linenum]
        self.linenum += 1
        return currline.split()

    def goto_label(self, label):
        """Jump to a given label.

        Will continue execution from that label when the next fetch occurs.
        """
        if logfile:
            logfile.write(f"GOTO: {label}" + "\n")
        for linenum, line in enumerate(self.lines):
            if line.split()[0] == f"{label}:":
                self.linenum = linenum
                return

        raise ValueError(f"Could not find label: {label}")


class Memory:
    """Represent main memory for the GPU.

    Very limited in scope, only used for loading initial parameters in the
    debug program.
    """
    def __init__(self):
        self.memory = {}

    def load(self, address):
        return self.memory[address]

    def store(self, address, data):
        self.memory[address] = data

class VectorMemory(ABC):
    """Interface for memory to be interfaced with vectors."""

    @abstractmethod
    def load(self, addrs):
        """Load data from backing memory."""

    @abstractmethod
    def store(self, addrs, data, mask):
        """Update the backing memory."""

class BufferMemory(VectorMemory):
    """Represent buffer memory for the AmdGpu

    Loads/stores are from/to separate regions (i.e. a load following a store to
    the same address won't see the stored data).

    Currently, the buffer is pre-loaded with data specific to the program I was
    debugging.

    Only supports vector loads and stores.

    Word addressed.
    """
    def __init__(self):
        # Loads retrieve data from 'memory'
        self.memory = list(range(100000))
        # Stores insert data into the output list
        self.output = [0] * 131072

    def load(self, addrs, index):
        """Load a vector from a given set of addresses.

        Only provide backing for the first 400000 bytes.
        """
        assert(index == 0 and "Buffer only supports index of 0")
        return [self.memory[addr // 4] if addr < 400000 else 0 for addr in addrs]

    def store(self, addrs, data, mask, index):
        """Store a vector into the output list.

        Data should be a list of size [4][64].
        """
        if logfile:
            logfile.write(f"Store Addrs: {addrs}" + "\n")
            logfile.write(f"Store Data: {data}" + "\n")
            logfile.write(f"Exec: {hex(mask)}" + "\n")

        for i in range(64):
            if (mask >> i) & 1:
                self.output[addrs[i]//4 + index] = data[i]


class ScratchMemory(VectorMemory):
    """Scratch memory, each address stores a single vector reegister."""

    def __init__(self):
        """Initialize empty dictionary to store data.

        Use a dictionary since I don't know/care about the size of scratch memory.
        """
        self.memory = {}

    def load(self, addr, index):
        """Retreive data from a given address in scratch memory."""
        return self.memory[addr + (4 * index)]

    def store(self, addr, data, mask, index):
        """Store data into scratch memory at the given address."""
        self.memory[addr + (4 * index)] = [v for v in data]

class Tester:
    """Collection of function to test the result of assembly run"""

    @staticmethod
    def get_expected_result():
        """Create the proper output data for testing.

        This is just copied from the code submitted in bug request.
        """
        expec = [-1] * (16 * 4 * 16 * 32);
        for i0 in range(16):
            for i1 in range(4):
                for i2 in range(16):
                    for i3 in range(32):
                        iOrig = i3 + 32 * i1
                        jOrig = i2 + 16 * i0
                        idx = i3 + 32 * (i2 + 16 * (i1 + 4 * i0))
                        if (iOrig >= 100) or (jOrig >= 250):
                            expec[idx] = 42
                        else:
                            expec[idx] = jOrig + 250 * iOrig
        return expec

    @staticmethod
    def check_buffer_result(buff):
        """Check the contents of a Buffer() to make sure the proper output data.

        Since not all workgroups are simulated, only check non-zero locations.
        """
        expec = Tester.get_expected_result()
        for i in range(len(expec)):
            if expec[i] != buff.output[i] and buff.output[i] != 0:
                raise ValueError(f"{i} {expec[i]} {buff.output[i]}")


class RegFile:
    """Represent the achritectural registers within and Amd GPU.

    256 vector registers, stored in list[256][64]

    512 scalar registers, stored in list[512]

    256 a registers, stored in list[256][64]
    """

    def __init__(self):
        self.v = [[0] * 64 for i in range(256)]
        self.a = [[0] * 64 for i in range(256)]
        self.s = [0] * 512

        # Conditional/exec registers
        self.exe = 0xFFFFFFFF_FFFFFFFF
        self.scc = 0
        self.vcc = 0

    def get_register(self, reg_name):
        """Return reg_file, reg_num."""
        if reg_name in ["exec", "vcc"]:
            return reg_name, None
        match = re.search(r"(\d+)", reg_name)
        reg_file = reg_name[0]
        if match and reg_file in ['s', 'v', 'a']:
            reg_num = match.group(1)
            return reg_file, int(reg_num)
        raise ValueError(f"Could not find register: {reg_name}")

    def set_regval(self, reg, new_val, index=None, offset=0):
        """Update the value for the provided register."""
        if reg[0:4] == "exec":
            self.exe = new_val
        elif reg[0:6] == "vcc_hi":
            self.vcc = (self.vcc & 0xFFFFFFFF) | (new_val << 32)
        elif reg[0:6] == "vcc_lo":
            self.vcc = (self.vcc & 0xFFFFFFFF00000000) | (new_val & 0xFFFFFFFF)
        elif reg[0:3] == "vcc":
            self.vcc = new_val
        elif reg[0:3] == "scc":
            self.scc = new_val
        else:
            reg_file, reg_num = self.get_register(reg)

            if reg_file == 's':
                self.s[reg_num+offset] = new_val
            elif reg_file == 'a':
                    for i in range(64):
                        if (self.exe >> i) & 1:
                            self.a[reg_num+offset][i] = new_val[i]
            else:
                if index is not None:
                    self.v[reg_num+offset][index] = new_val
                else:
                    for i in range(64):
                        if (self.exe >> i) & 1:
                            self.v[reg_num+offset][i] = new_val[i]
        if logfile:
            logfile.write(f"WRITE: {reg}: {self.get_regval(reg)}" + "\n")

    def get_regval(self, reg, offset=0):
        """Retrieve the value for the given register."""
        if reg[0:4] == "exec":
            return self.exe
        elif reg[0:6] == "vcc_hi":
            return  self.vcc >> 32
        elif reg[0:6] == "vcc_lo":
            return self.vcc & 0xFFFFFFFF
        elif reg[0:3] == "vcc":
            return self.vcc
        elif reg[0:3] == "scc":
            return self.scc
        else:
            reg_file, reg_num = self.get_register(reg)
            if reg_file == 's':
                return self.s[reg_num + offset]
            elif reg_file == 'a':
                return self.a[reg_num + offset]
            else:
                return self.v[reg_num + offset]


class AmdGpu:
    """Represents a single work group on an Amd GPU.

    TODO: Simulate all work groups in parallel.
    """
    def __init__(self, filename, workgroup_id):
        self.buff = BufferMemory()
        self.scratch_memory = ScratchMemory()
        self.program = Program(filename)

        self.regfile = RegFile()
        self.regfile.v[0] = [i for i in range(64)] # Initialize to hold workitem.id()
        self.regfile.s[6] = workgroup_id # s6 holds workgroup.id()


        # I think think should hold the pointers to the different buffer data
        # locations, but I don't care since for my program there is a single
        # buffer load location, and separate buffer store location
        self.memory = Memory()
        self.memory.store(0, 0)
        self.memory.store(8, 0)


        # Execute the program
        count = 0
        if logfile:
            logfile.write(str(count) + "\n")
        while not self.operate(self.program.get_next_instruction()):
            if logfile:
                logfile.write(str(count) + "\n")
            count += 1
            pass

        Tester.check_buffer_result(self.buff)

    def operate(self, instruction):
        """Execute a single instruction.

        Performs necessary memory/register file updates.
        """

        if logfile:
            logfile.write(str(instruction) + "\n")

        operands = instruction[1:]
        instruction = instruction[0]

        class Instruction(ABC):
            """Base class for all Amd GPU instruction functions.

            Includes the helper functions for parsing operands.
            """
            @abstractmethod
            def execute(operands, regfile):
                pass

            @staticmethod
            def get_int(val):
                """Parse an immediate value from an assembly instruction.

                - Cleans up whitespace/trailing charecters before converting.
                - Mainly here to handle intergers passed in either base 10 or 16.
                """
                val = val.strip()
                if val[-1] == ",":
                    val = val[:-1]
                if len(val) > 1 and val[0:2] == "0x":
                    return int(val, 16)
                return int(val)

            @staticmethod
            def get_int_or_reg(name, regfile):
                """Return the value for an operand that could be a register or an integer.

                TODO: Make this less hacky.
                """
                if name[0].isdigit() or name[0] == "-":
                    return Instruction.get_int(name)
                return regfile.get_regval(name)

            @staticmethod
            def immediate_operands(operands, regfile):
                """Parse operands for operations that have the format: dest = source op imm."""
                destination_reg = operands[0]
                source_data = regfile.get_regval(operands[1])
                imm = Instruction.get_int(operands[2])
                return destination_reg, source_data, imm

            @staticmethod
            def immediate_operands_rev(operands, regfile):
                """Parse operands for operations that have the format: dest = source op imm."""
                destination_reg = operands[0]
                source_data = regfile.get_regval(operands[2])
                imm = Instruction.get_int(operands[1])
                return destination_reg, source_data, imm

        class Nop:
            @staticmethod
            def execute(operands, regfile):
                pass

        class EndProgram:
            @staticmethod
            def execute(operands, regfile):
                return True

        class SLoadDwordX2:
            @staticmethod
            def execute(operands, regfile):
                destination_reg = operands[0]
                address_reg = operands[1]
                offset = operands[2]

                address = regfile.get_regval(address_reg) + Instruction.get_int(offset)
                regfile.set_regval(destination_reg, self.memory.load(address))

        class Branch:
            def __init__(self, program):
                self.program = program

            def execute(self, operands, regfile):
                label = operands[0]
                self.program.goto_label(label)

        class Mov:
            @staticmethod
            def execute(operands, regfile):
                destination_reg = operands[0]
                source_reg = Instruction.get_int_or_reg(operands[1], regfile)
                regfile.set_regval(destination_reg, source_reg)

        class VectorMov:
            def __init__(self, count):
                self.count = count

            def execute(self, operands, regfile):
                dest_reg = operands[0]
                source_reg = operands[1]
                if source_reg[0].isdigit():
                    source_data = [Instruction.get_int(source_reg) for _ in range(self.count)]
                    scalar_source = True
                else:
                    s_file, s_num = regfile.get_register(source_reg)
                    scalar_source = s_file == 's'
                    source_data = [regfile.get_regval(f"{s_file}{s_num + i}")
                                   for i in range(self.count)]
                d_file, d_num = regfile.get_register(dest_reg)
                dest_regs = [f"{d_file}{d_num + i}" for i in range(self.count)]
                for i in range(self.count):
                    if scalar_source and d_file in ['v', 'a']:
                        result = [source_data[i]] * 64
                    else:
                        result =  source_data[i]
                    regfile.set_regval(dest_regs[i], result)

        class VectorImmediate:
            def __init__(self, operation):
                self.operation = operation

            def execute(self, operands, regfile):
                destination_reg, source_data, imm = Instruction.immediate_operands_rev(operands, regfile)
                result_data = [self.operation(val, imm) for val in source_data]
                regfile.set_regval(destination_reg, result_data)

        class VectorVector(Instruction):
            def __init__(self, operation):
                self.operation = operation

            def execute(self, operands, regfile):
                destination_reg = operands[0]
                source_reg0 = regfile.get_regval(operands[1])
                source_reg1 = regfile.get_regval(operands[2])
                result_data = [self.operation(source_reg0[i], source_reg1[i]) for i in range(64)]
                regfile.set_regval(destination_reg, result_data)

        class ScalarScalar:
            def __init__(self, operation):
                self.operation = operation

            def execute(self, operands, regfile):
                destination_reg = operands[0]
                source_data0 = regfile.get_regval(operands[1])
                source_data1 = Instruction.get_int_or_reg(operands[2], regfile)
                regfile.set_regval(destination_reg,
                                   self.operation(source_data0, source_data1))

        class SccCmp:
            def __init__(self, operation):
                self.operation = operation

            def execute(self, operands, regfile):
                source_data = regfile.get_regval(operands[0])
                imm = Instruction.get_int(operands[1])
                regfile.set_regval("scc", self.operation(source_data, imm))

        class BranchCondition:
            def __init__(self, program, operation):
                self.program = program
                self.operation = operation

            def execute(self, operands, regfile):
                label = operands[0]
                if self.operation(regfile):
                    self.program.goto_label(label)

        class VectorWritelane:
            @staticmethod
            def execute(operands, regfile):
                destination_reg, source_data, imm = Instruction.immediate_operands(operands, regfile)
                regfile.set_regval(destination_reg, source_data, imm)

        class VectorReadlane:
            @staticmethod
            def execute(operands, regfile):
                destination_reg, source_data, imm = Instruction.immediate_operands(operands, regfile)
                regfile.set_regval(destination_reg, source_data[imm])

        class VectorCmp:
            def __init__(self, operation):
                self.operation = operation

            def execute(self, operands, regfile):
                destination_reg, source_data, imm = Instruction.immediate_operands_rev(operands, regfile)
                mask = 0
                for idx, val in enumerate(source_data):
                    if self.operation(val, imm):
                        mask |= 1 << idx
                regfile.set_regval(destination_reg, mask)

        class VectorCndMsk:
            @staticmethod
            def execute(operands, regfile):
                destination_reg = operands[0]
                source_reg = operands[3]
                imm_false = Instruction.get_int(operands[1])
                imm_true = Instruction.get_int(operands[2])
                source_val = regfile.get_regval(source_reg)
                regfile.set_regval(destination_reg, [imm_true if ((source_val >> i) & 1) else imm_false for i in range(64)])

        class SAndSaveExec:
            @staticmethod
            def execute(operands, regfile):
                destination_reg = operands[0]
                exe = regfile.get_regval("exec")
                source_data = regfile.get_regval(operands[1])
                regfile.set_regval(destination_reg, exe)
                regfile.set_regval("exec", source_data & exe)

        class VectorLshl:
            def __init__(self, operation):
                self.operation = operation

            def execute(self, operands, regfile):
                destination_reg = operands[0]
                s_source_data = regfile.get_regval(operands[1])
                imm = Instruction.get_int(operands[2])
                v_source_data = regfile.get_regval(operands[3])
                imm = s_source_data << imm
                regfile.set_regval(destination_reg, [self.operation(imm, val) for val in v_source_data])

        class ScalarCSelect:
            @staticmethod
            def execute(operands, regfile):
                destination_reg = operands[0]
                imm0 = Instruction.get_int(operands[1])
                imm1 = Instruction.get_int(operands[1])
                scc = regfile.get_regval("scc")
                regfile.set_regval(destination_reg, imm0 if scc == 1 else imm1)

        class VectorLoad:
            def __init__(self, count, memory):
                self.count = count
                self.memory = memory

            def execute(self, operands, regfile):
                addr = self.get_addr(operands, regfile)
                dest_reg = operands[0]
                d_file, d_num = regfile.get_register(dest_reg)
                for i in range(self.count):
                    load_data = self.memory.load(addr, i)
                    regfile.set_regval(f"{d_file}{d_num+i}", load_data)

        class ScratchLoadDword(VectorLoad):
            def get_addr(self, operands, regfile):
                if operands[3].split(':')[0] == "offset":
                    offset = Instruction.get_int(operands[3].split(':')[1])
                else:
                    offset = 0
                return offset

        class BufferLoadDword(VectorLoad):
            def get_addr(self, operands, regfile):
                assert(operands[3] == '0' and operands[4] == "offen")
                if (len(operands) > 5 and len(operands[5].split(":")) == 2):
                    offset = int(operands[5].split(":")[1])
                else:
                    offset = 0
                addrs = [v + offset for v in regfile.get_regval(operands[1])]
                return addrs

        class VectorStore:
            def __init__(self, count, memory):
                self.count = count
                self.memory = memory

            def execute(self, operands, regfile):
                addr = self.get_addr(operands, regfile)
                mask = self.get_mask(regfile)
                s_file, s_num = regfile.get_register(operands[self.source_loc])
                for i in range(self.count):
                    source_data = regfile.get_regval(f"{s_file}{s_num+i}")
                    self.memory.store(addr, source_data, mask, i)

        class BufferStoreDword(VectorStore):
            source_loc = 0

            def get_addr(self, operands, regfile):
                if (len(operands) > 5 and len(operands[5].split(":")) == 2):
                    offset = int(operands[5].split(":")[1])
                else:
                    offset = 0
                base_addrs = regfile.get_regval(operands[1])
                addrs = [addr + offset for addr in base_addrs]
                return addrs

            def get_mask(self, regfile):
                return regfile.get_regval("exec")

        class ScratchStoreDword(VectorStore):
            source_loc = 1

            def get_addr(self, operands, regfile):
                if operands[3].split(':')[0] == "offset":
                    offset = Instruction.get_int(operands[3].split(':')[1])
                else:
                    offset = 0
                return offset

            def get_mask(self, regfile):
                return None

        instructions = {
            "s_waitcnt": Nop,
            "s_endpgm": EndProgram,
            "s_load_dwordx2": SLoadDwordX2,
            "s_branch": Branch(self.program),
            "s_mov_b32": Mov,
            "s_mov_b64": Mov,
            "s_lshl_b32": ScalarScalar(lambda x, y: x << y),
            "s_xor_b32": ScalarScalar(lambda x, y: x ^ y),
            "s_xor_b64": ScalarScalar(lambda x, y: x ^ y),
            "s_min_u32": ScalarScalar(lambda x, y: min(x, y)),
            "v_lshlrev_b32_e32": VectorImmediate(lambda x, y: (x << y) & 0xFFFFFFFF),
            "v_sub_u32_e32": VectorImmediate(lambda x, y: y - x),
            "v_min_i32_e32": VectorVector(lambda x, y: min(x, y)),
            "v_mul_u32_u24_e32": VectorImmediate(lambda x, y: x * y),
            "s_sub_i32": ScalarScalar(lambda x, y: x - y),
            "s_nop": Nop,
            "v_add_u32_e32": VectorImmediate(lambda x, y: x + y),
            "s_andn2_b64": ScalarScalar(lambda x, y: x & ~y),
            "s_and_b32": ScalarScalar(lambda x, y: x & y),
            "s_and_b64": ScalarScalar(lambda x, y: x & y),
            "s_cbranch_execz": BranchCondition(self.program, lambda x: x.get_regval("exec") == 0),
            "s_cbranch_vccz": BranchCondition(self.program, lambda x: x.get_regval("vcc") == 0),
            "s_cbranch_vccnz": BranchCondition(self.program, lambda x: x.get_regval("vcc") != 0),
            "s_cbranch_scc1": BranchCondition(self.program, lambda x: x.get_regval("scc") == 1),
            "s_cbranch_scc0": BranchCondition(self.program, lambda x: x.get_regval("scc") == 0),
            "s_cmp_gt_u32": SccCmp(lambda x, y: 1 if x > y else 0),
            "s_cmp_lg_u32": SccCmp(lambda x, y: 1 if x != y else 0),
            "s_cmp_eq_u32": SccCmp(lambda x, y: 1 if x == y else 0),
            "v_mov_b32_e32": VectorMov(1),
            "s_or_b32": ScalarScalar(lambda x, y: x | y),
            "s_or_b64": ScalarScalar(lambda x, y: x | y),
            "v_writelane_b32": VectorWritelane,
            "v_readlane_b32": VectorReadlane,
            "v_cmp_gt_u32_e64": VectorCmp(lambda x, y: x < y),
            "v_cmp_lt_i32_e64": VectorCmp(lambda x, y: x > y),
            "v_cmp_ne_u32_e64": VectorCmp(lambda x, y: x != y),
            "v_cndmask_b32_e64": VectorCndMsk,
            "s_and_saveexec_b64": SAndSaveExec,
            "v_lshl_add_u32": VectorLshl(lambda x, y: x + y),
            "v_lshl_or_b32": VectorLshl(lambda x, y: x | y),
            "v_mov_b64_e32": VectorMov(2),
            "s_cselect_b64": ScalarCSelect,
            "v_accvgpr_read_b32": VectorMov(1),
            "v_mov_b32_e32": VectorMov(1),
            "v_accvgpr_mov_b32": VectorMov(1),
            "v_accvgpr_write_b32": VectorMov(1),
            "scratch_store_dwordx4": ScratchStoreDword(4, self.scratch_memory),
            "scratch_store_dwordx2": ScratchStoreDword(2, self.scratch_memory),
            "scratch_load_dwordx4": ScratchLoadDword(4, self.scratch_memory),
            "scratch_load_dwordx2": ScratchLoadDword(2, self.scratch_memory),
            "buffer_store_dwordx4": BufferStoreDword(4, self.buff),
            "buffer_load_dword": BufferLoadDword(1, self.buff),
        }

        if instruction in instructions:
            instruction = instructions[instruction]
            return instruction.execute(operands, self.regfile)
        else:
            raise ValueError(f"Could not find instruction: {instruction}")
        return False


if __name__ == "__main__":
    """Emulate the given program on an Amd GPU."""
    parser = argparse.ArgumentParser(description="Simple amdgpu emulator.")
    parser.add_argument("--workgroup-id", default=0, type=int,
                        help="ID of the workgroup to simulate")
    parser.add_argument("--program", required=True,
                        help="Assembly program to emulate")
    parser.add_argument("--logfile", type=str,
                        help="File to print the instruction trace to.")

    args = parser.parse_args()
    if args.logfile:
        logfile = open(args.logfile, "w")
    else:
        logfile = None

    gpu = AmdGpu(args.program, args.workgroup_id)

    if logfile:
        logfile.close()
